"""
MCP-protocol OAuth: a "Sign in" button inside ChatGPT, Claude and Cursor.

What this adds, and what it deliberately does not touch
------------------------------------------------------
Day 1 shipped `/connect`: sign in with Google in a browser, paste a RapidAPI
key, copy back a URL carrying an `fpk_` token. That works, but no MCP client
will ever *start* a sign-in on its own, because nothing on the wire tells it
one is available. A client only begins an OAuth flow when a request is
answered with `401` and a `WWW-Authenticate: Bearer resource_metadata=...`
header, which is exactly what `/mcp` must never do: every paying caller today
authenticates with a RapidAPI key and no bearer token, and challenging them
is an outage, not a feature.

So this file adds a SECOND endpoint, `/mcp/oauth`, which always challenges:

    /mcp        unchanged forever. Keys, `fpk_` tokens, anonymous
                tools/list, Smithery's config blob. No 401, no challenge.
    /mcp/oauth  same tools, same product-per-Host routing, but Bearer-only:
                no token means 401 + the challenge header, which is the
                signal that makes a client show a Sign in button.

Two endpoints rather than one negotiated endpoint because "challenge only
when the caller looks like it could handle it" is a guess about a client, and
a wrong guess breaks a paying integration. A separate URL is a decision the
user makes when they paste it, and it is the URL we publish in directories.

The flow, end to end
--------------------
1. The client GETs `/mcp/oauth`, gets 401 plus
   `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp/oauth"`.
2. It fetches that document (RFC 9728), learns the authorization server, and
   fetches `/.well-known/oauth-authorization-server` (RFC 8414).
3. It registers itself at `/oauth/register` (RFC 7591). Registration is open,
   as the MCP spec requires: a client_id on its own authorises nothing.
4. It opens `/connect/authorize` in a browser with PKCE S256.
5. That page hands off to the day-1 Google sign-in when there is no session,
   then asks the human to approve THIS client by name.
6. Approval redirects back to the client's `redirect_uri` with a code.
7. The client exchanges the code at `/oauth/token` for an access token
   (1 hour) and a refresh token (30 days).
8. Every `/mcp/oauth` call carries `Authorization: Bearer fpo_…`. The gate
   resolves it to a Google `sub`, and the tool call looks that `sub` up in
   the day-1 key store to find the user's RapidAPI key. A user who signed in
   but never pasted a key gets the "connect at /connect" error, with the URL.

Why the authorization endpoint lives under /connect
---------------------------------------------------
`/connect/authorize`, not `/authorize`. The day-1 session cookie is scoped
`Path=/connect` on purpose -- so it can never be attached to a `/mcp`
request, because a session cookie is not a credential this server accepts
there. Putting the authorization endpoint anywhere else would mean either
widening that cookie's path (giving up the property) or running a second
sign-in (asking the user to authenticate twice). Under `/connect` it reuses
the session that is already there.

Why opaque tokens and not JWTs
------------------------------
A JWT would remove one database read per tool call. It would also make
"revoke" a lie: a signed token stays valid until it expires no matter what we
decide afterwards, and this feature has to support Disconnect, RFC 7009
revocation, and refresh-token rotation -- all of which are claims about a row
existing. The database read is already on the path (the RapidAPI key lookup
is a second one), so the JWT saves nothing that matters here. Tokens are 32
bytes of `os.urandom`, stored as SHA-256 hashes, and never written to a log.

Why `fpo_` / `fpr_` prefixes
----------------------------
`Authorization: Bearer <value>` is ALSO one of the ways a RapidAPI key
arrives (credentials.py). Without a prefix that can only be ours, an OAuth
access token in that header would be forwarded to RapidAPI, rejected, and
reported to the user as "your key was refused" -- the same trap `fpk_` was
introduced to close on day 1, so the same fix: the prefixes are listed in
`credentials.OUR_TOKEN_PREFIXES` and filtered at the one place every channel
passes through.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from .keystore import PROVIDER_GOOGLE
from .oauthstore import (
    AuthCode,
    OAuthClient,
    OAuthStore,
    OAuthStoreError,
    TokenRecord,
    build_oauth_store,
    hash_secret,
)
from .webauth import WebAuthError, sign_payload, verify_payload

logger = logging.getLogger(__name__)

# ── the shape of the deployment ──────────────────────────────────────────

#: The always-challenging MCP endpoint. `/mcp` is untouched.
MCP_OAUTH_PATH = "/mcp/oauth"
#: What the gate rewrites the path to before handing the request on. FastMCP
#: mounts a plain `Route("/mcp")`, not a Mount, so `/mcp/oauth` reaches
#: nothing on its own -- the rewrite is what makes "same tools" literal
#: rather than a second copy of the tool registry.
MCP_PATH = "/mcp"

AUTHORIZE_PATH = "/connect/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"

#: One scope. A second one would be a promise that some tokens can do less
#: than others, and nothing in this server enforces such a split.
DEFAULT_SCOPE = "flightpowers:search"

ACCESS_TOKEN_PREFIX = "fpo_"
REFRESH_TOKEN_PREFIX = "fpr_"
CODE_PREFIX = "fpc_"
#: Client ids are not secrets, but they share a channel with values that are,
#: so they get a prefix of their own rather than one a code could be mistaken
#: for at a glance in a log.
CLIENT_ID_PREFIX = "fpcl_"

#: RFC 6749 §4.1.2 says a code SHOULD be short-lived and names 10 minutes as
#: the maximum. This is that maximum, not a number picked for comfort.
CODE_TTL_SECONDS = 10 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
#: How long the signed blob on the consent form stays valid. Long enough to
#: read the page, short enough that a form left open in a tab is not a
#: standing authorisation.
CONSENT_TTL_SECONDS = 15 * 60

#: Injected by the gate after a token validates, and STRIPPED from every
#: inbound request before anything else runs. The strip is what makes the
#: injection trustworthy: without it, any caller could send these headers to
#: plain `/mcp` and be served from a stranger's stored key.
def header_safe(value: str) -> str:
    """A value fit to be an HTTP header.

    The `sub`, provider and client id all come out of our own database, so
    this is belt-and-braces -- but they are ATTACKER-INFLUENCED in the sense
    that a Google account id and a registered client id both originate
    outside this process, and a CR or LF in one would be a response-splitting
    bug in the layer below. Printable ASCII only, truncated.
    """
    return "".join(ch for ch in value if " " <= ch <= "~")[:256]


SUBJECT_HEADER = "x-fp-oauth-subject"
PROVIDER_HEADER = "x-fp-oauth-provider"
CLIENT_HEADER = "x-fp-oauth-client"
IDENTITY_HEADERS = (SUBJECT_HEADER, PROVIDER_HEADER, CLIENT_HEADER)


class OAuthError(Exception):
    """One OAuth 2.0 error, with the code the RFC names.

    `redirectable` says whether the client's `redirect_uri` has been
    validated yet. It has not for a bad `client_id` or a bad `redirect_uri`,
    and RFC 6749 §4.1.2.1 is explicit that those two must NOT be redirected --
    doing so turns the authorization endpoint into an open redirector.
    """

    def __init__(
        self, code: str, description: str = "", *, redirectable: bool = True,
        status: int = 400,
    ) -> None:
        super().__init__(description or code)
        self.code = code
        self.description = description
        self.redirectable = redirectable
        self.status = status

    def as_dict(self) -> dict[str, str]:
        body = {"error": self.code}
        if self.description:
            body["error_description"] = self.description
        return body


# ── tokens ───────────────────────────────────────────────────────────────


def mint(prefix: str) -> str:
    """One opaque secret. 32 bytes of urandom, URL-safe, prefixed."""
    return prefix + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """S256, exactly as RFC 7636 §4.6 defines it."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def bearer_token(authorization: str | None) -> str:
    """The token out of an `Authorization: Bearer …` header, or "".

    Case-insensitive on the scheme, because RFC 7235 says the scheme is
    case-insensitive and at least one MCP client sends `bearer`.
    """
    raw = (authorization or "").strip()
    if not raw:
        return ""
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


# ── the feature, assembled ───────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthSupport:
    """Everything the OAuth endpoints and the /mcp/oauth gate need.

    Built once per product in `build_server`, or not at all. `None` is the
    normal state: a deployment that has not been given the day-1 variables
    registers nothing and changes no request path.
    """

    store: OAuthStore
    auth: Any               # webauth.GoogleWebAuth -- the browser sign-in
    origin: str             # this product's canonical scheme+host
    product: str

    # ── identity of this authorization server / resource ─────────────────

    @property
    def issuer(self) -> str:
        return self.origin.rstrip("/")

    @property
    def resource_url(self) -> str:
        return f"{self.issuer}{MCP_OAUTH_PATH}"

    @property
    def resource_metadata_url(self) -> str:
        # The path-scoped form. A client that read the 401 challenge follows
        # this URL literally; the unscoped path is served too, for clients
        # that construct it themselves from the origin.
        return f"{self.issuer}{PROTECTED_RESOURCE_PATH}{MCP_OAUTH_PATH}"

    # ── metadata documents ───────────────────────────────────────────────

    def protected_resource_metadata(self) -> dict[str, Any]:
        """RFC 9728. What resource this is and who can authorise it."""
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.issuer],
            "scopes_supported": [DEFAULT_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": f"FlightPowers {self.product} MCP",
            "resource_documentation": f"{self.issuer}/",
            "resource_policy_uri": f"{self.issuer}/privacy",
            "resource_tos_uri": f"{self.issuer}/terms",
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        """RFC 8414. Note `code_challenge_methods_supported` is S256 only:
        `plain` is in the RFC and is not offered, because a challenge that is
        the verifier protects nothing."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}{AUTHORIZE_PATH}",
            "token_endpoint": f"{self.issuer}{TOKEN_PATH}",
            "registration_endpoint": f"{self.issuer}{REGISTER_PATH}",
            "revocation_endpoint": f"{self.issuer}{REVOKE_PATH}",
            "scopes_supported": [DEFAULT_SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "revocation_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256"],
            "service_documentation": f"{self.issuer}/",
            "op_policy_uri": f"{self.issuer}/privacy",
            "op_tos_uri": f"{self.issuer}/terms",
        }

    # ── resource indicator (RFC 8707) ────────────────────────────────────

    def resource_matches(self, requested: str) -> bool:
        """Whether a client's `resource=` names this server.

        Deliberately forgiving about the path and the trailing slash, strict
        about the origin. Clients in the wild send the origin, the `/mcp`
        path and the `/mcp/oauth` path for the same server, and rejecting two
        of those would break a flow over a formatting difference. Rejecting a
        different HOST is the part that matters: that is the confused-deputy
        case RFC 8707 exists for.
        """
        if not requested:
            return True
        want = urlsplit(self.resource_url)
        got = urlsplit(requested.strip())
        if not got.scheme or not got.netloc:
            return False
        if (got.scheme.lower(), got.netloc.lower()) != (
            want.scheme.lower(),
            want.netloc.lower(),
        ):
            return False
        path = got.path.rstrip("/")
        return path in ("", MCP_PATH, MCP_OAUTH_PATH)

    # ── dynamic client registration ──────────────────────────────────────

    async def register(self, body: dict[str, Any]) -> dict[str, Any]:
        """RFC 7591. Returns the registration response to send back.

        Open registration, which the MCP spec requires and which is safe
        here for one reason worth stating: a `client_id` authorises nothing.
        Every flow through it still ends at a consent page that a human has
        to be signed into Google to see and has to press a button on. The
        row is a name and a redirect URI, not a permission.
        """
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            raise OAuthError(
                "invalid_redirect_uri", "redirect_uris must be a non-empty list"
            )
        cleaned: list[str] = []
        for raw in uris:
            if not isinstance(raw, str) or not raw.strip():
                raise OAuthError("invalid_redirect_uri", "a redirect_uri was empty")
            uri = raw.strip()
            if not _redirect_uri_allowed(uri):
                raise OAuthError(
                    "invalid_redirect_uri",
                    f"redirect_uri {uri!r} is not an https URL, a loopback "
                    "http URL, or a private-use scheme",
                )
            cleaned.append(uri)

        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list):
            raise OAuthError("invalid_client_metadata", "grant_types must be a list")
        unsupported = set(grant_types) - {"authorization_code", "refresh_token"}
        if unsupported:
            raise OAuthError(
                "invalid_client_metadata",
                f"unsupported grant_types: {', '.join(sorted(unsupported))}",
            )
        response_types = body.get("response_types") or ["code"]
        if isinstance(response_types, list) and set(response_types) - {"code"}:
            raise OAuthError(
                "invalid_client_metadata", "only response_type=code is supported"
            )

        method = str(body.get("token_endpoint_auth_method") or "none").strip()
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError(
                "invalid_client_metadata",
                f"token_endpoint_auth_method {method!r} is not supported",
            )

        client_id = mint(CLIENT_ID_PREFIX)
        secret = "" if method == "none" else mint("fps_")
        client = OAuthClient(
            client_id=client_id,
            client_name=str(body.get("client_name") or "an MCP client")[:120],
            redirect_uris=tuple(cleaned),
            token_endpoint_auth_method=method,
            scope=str(body.get("scope") or DEFAULT_SCOPE),
            client_secret_hash=hash_secret(secret) if secret else "",
            created_at=time.time(),
            metadata={
                k: v
                for k, v in body.items()
                if k in ("client_uri", "logo_uri", "software_id", "software_version")
                and isinstance(v, str)
            },
        )
        await self.store.register_client(client)
        logger.info(
            "registered MCP client %s (%s)", client_id, client.client_name
        )
        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(client.created_at),
            "client_name": client.client_name,
            "redirect_uris": list(client.redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": client.scope,
        }
        if secret:
            # Returned once and never again -- only its hash is stored, so
            # there is nothing to re-issue it from. `0` means "does not
            # expire", per RFC 7591 §3.2.1.
            response["client_secret"] = secret
            response["client_secret_expires_at"] = 0
        return response

    # ── the authorization request ────────────────────────────────────────

    async def read_authorize_request(
        self, params: dict[str, str]
    ) -> tuple[OAuthClient, dict[str, str]]:
        """Validate `/connect/authorize` query parameters.

        Raises OAuthError with `redirectable=False` for the two failures that
        must render a page instead of bouncing (unknown client, unregistered
        redirect_uri), and with `redirectable=True` for everything after
        that.
        """
        client_id = (params.get("client_id") or "").strip()
        if not client_id:
            raise OAuthError(
                "invalid_request", "client_id is missing", redirectable=False
            )
        try:
            client = await self.store.get_client(client_id)
        except OAuthStoreError as exc:
            logger.warning("client lookup failed: %s", exc)
            raise OAuthError(
                "temporarily_unavailable",
                "The sign-in store is not reachable right now.",
                redirectable=False,
                status=503,
            ) from exc
        if client is None:
            raise OAuthError(
                "invalid_client",
                "That client is not registered with this server.",
                redirectable=False,
            )

        redirect_uri = (params.get("redirect_uri") or "").strip()
        if not redirect_uri:
            if len(client.redirect_uris) != 1:
                raise OAuthError(
                    "invalid_request",
                    "redirect_uri is required when a client registered more "
                    "than one",
                    redirectable=False,
                )
            redirect_uri = client.redirect_uris[0]
        elif redirect_uri not in client.redirect_uris:
            # Exact string match, not a prefix or a host match: a loose
            # comparison here is how authorization codes get delivered to
            # somebody else's URL.
            raise OAuthError(
                "invalid_request",
                "That redirect_uri is not registered for this client.",
                redirectable=False,
            )

        if (params.get("response_type") or "").strip() != "code":
            raise OAuthError(
                "unsupported_response_type", "only response_type=code is supported"
            )

        challenge = (params.get("code_challenge") or "").strip()
        method = (params.get("code_challenge_method") or "").strip()
        if not challenge:
            raise OAuthError(
                "invalid_request",
                "PKCE is required: send code_challenge with "
                "code_challenge_method=S256",
            )
        if method != "S256":
            raise OAuthError(
                "invalid_request",
                "code_challenge_method must be S256; plain is not accepted",
            )

        resource = (params.get("resource") or "").strip()
        if not self.resource_matches(resource):
            raise OAuthError(
                "invalid_target",
                f"this server is {self.resource_url}, not {resource}",
            )

        return client, {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "scope": (params.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
            "state": params.get("state") or "",
            "resource": resource or self.resource_url,
        }

    # ── the consent form's signed blob ───────────────────────────────────

    def seal_request(self, request: dict[str, str], sub: str, now: float | None = None) -> str:
        """The validated authorization request, signed, for the hidden field.

        Signed rather than re-read from the form on POST: everything in it has
        already been checked against the registered client, and re-validating
        a value the browser could have edited is how a consent page ends up
        approving a request the user never saw. Bound to `sub` as well, so a
        blob minted for one account cannot be posted by another.
        """
        payload = dict(request)
        payload["typ"] = "authz"
        payload["sub"] = sub
        payload["exp"] = int((now if now is not None else time.time())) + CONSENT_TTL_SECONDS
        return sign_payload(payload, self.auth.session_secret)

    def open_request(self, sealed: str, sub: str, now: float | None = None) -> dict[str, str]:
        payload = verify_payload(sealed, self.auth.session_secret, now=now)
        if payload.get("typ") != "authz":
            raise WebAuthError("wrong token type")
        if not hmac.compare_digest(str(payload.get("sub", "")), sub):
            raise WebAuthError("this form belongs to a different account")
        return {
            k: str(v)
            for k, v in payload.items()
            if k in ("client_id", "redirect_uri", "code_challenge", "scope", "state", "resource")
        }

    # ── issuing ──────────────────────────────────────────────────────────

    async def issue_code(
        self, request: dict[str, str], sub: str, now: float | None = None
    ) -> str:
        now = now if now is not None else time.time()
        code = mint(CODE_PREFIX)
        await self.store.put_code(
            AuthCode(
                code_hash=hash_secret(code),
                client_id=request["client_id"],
                redirect_uri=request["redirect_uri"],
                code_challenge=request["code_challenge"],
                scope=request.get("scope", DEFAULT_SCOPE),
                user_sub=sub,
                provider=PROVIDER_GOOGLE,
                resource=request.get("resource", self.resource_url),
                expires_at=now + CODE_TTL_SECONDS,
            )
        )
        return code

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        user_sub: str,
        provider: str,
        scope: str,
        resource: str,
        now: float,
    ) -> dict[str, Any]:
        access = mint(ACCESS_TOKEN_PREFIX)
        refresh = mint(REFRESH_TOKEN_PREFIX)
        await self.store.put_token(
            TokenRecord(
                token_hash=hash_secret(access),
                kind="access",
                client_id=client_id,
                user_sub=user_sub,
                provider=provider,
                scope=scope,
                resource=resource,
                expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
            )
        )
        await self.store.put_token(
            TokenRecord(
                token_hash=hash_secret(refresh),
                kind="refresh",
                client_id=client_id,
                user_sub=user_sub,
                provider=provider,
                scope=scope,
                resource=resource,
                expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
            )
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": scope,
        }

    # ── the token endpoint ───────────────────────────────────────────────

    async def _authenticate_client(
        self, form: dict[str, str], authorization: str | None
    ) -> OAuthClient:
        """Whichever of the three registered methods this client uses.

        A public client proves nothing here; PKCE is what stands in for a
        secret, and this server requires it from every client, so a public
        client is not a weaker case -- it is the normal one.
        """
        client_id = (form.get("client_id") or "").strip()
        client_secret = (form.get("client_secret") or "").strip()

        raw = (authorization or "").strip()
        if raw.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(raw[6:].strip() + "==").decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
                raise OAuthError(
                    "invalid_client", "malformed Basic credentials", status=401
                ) from exc
            basic_id, _, basic_secret = decoded.partition(":")
            # Body and header disagreeing is a client bug worth naming rather
            # than silently preferring one.
            if client_id and basic_id and client_id != basic_id:
                raise OAuthError(
                    "invalid_client",
                    "client_id in the body and in the Authorization header "
                    "disagree",
                    status=401,
                )
            client_id = client_id or basic_id
            client_secret = client_secret or basic_secret

        if not client_id:
            raise OAuthError("invalid_client", "client_id is missing", status=401)
        try:
            client = await self.store.get_client(client_id)
        except OAuthStoreError as exc:
            logger.warning("client lookup failed at the token endpoint: %s", exc)
            raise OAuthError(
                "temporarily_unavailable",
                "The sign-in store is not reachable right now.",
                status=503,
            ) from exc
        if client is None:
            raise OAuthError("invalid_client", "unknown client_id", status=401)
        if client.is_public:
            return client
        if not client_secret or not hmac.compare_digest(
            client.client_secret_hash, hash_secret(client_secret)
        ):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
        return client

    async def token(
        self,
        form: dict[str, str],
        authorization: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        client = await self._authenticate_client(form, authorization)
        grant = (form.get("grant_type") or "").strip()
        if grant == "authorization_code":
            return await self._authorization_code_grant(client, form, now)
        if grant == "refresh_token":
            return await self._refresh_token_grant(client, form, now)
        raise OAuthError(
            "unsupported_grant_type",
            f"grant_type {grant!r} is not supported; use authorization_code "
            "or refresh_token",
        )

    async def _authorization_code_grant(
        self, client: OAuthClient, form: dict[str, str], now: float
    ) -> dict[str, Any]:
        code = (form.get("code") or "").strip()
        if not code:
            raise OAuthError("invalid_request", "code is missing")
        # Consumed BEFORE anything is checked. A code presented with a wrong
        # verifier has still been presented, and leaving it alive would let
        # an attacker who intercepted it keep guessing.
        record = await self.store.consume_code(hash_secret(code))
        if record is None:
            raise OAuthError(
                "invalid_grant", "that code is unknown, already used, or expired"
            )
        if now >= record.expires_at:
            raise OAuthError("invalid_grant", "that code has expired")
        if record.client_id != client.client_id:
            raise OAuthError("invalid_grant", "that code was issued to another client")

        redirect_uri = (form.get("redirect_uri") or "").strip()
        if redirect_uri and redirect_uri != record.redirect_uri:
            raise OAuthError(
                "invalid_grant", "redirect_uri does not match the authorization request"
            )

        verifier = (form.get("code_verifier") or "").strip()
        if not verifier:
            raise OAuthError("invalid_grant", "code_verifier is missing")
        if not hmac.compare_digest(pkce_challenge(verifier), record.code_challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match the challenge")

        resource = (form.get("resource") or "").strip()
        if resource and not self.resource_matches(resource):
            raise OAuthError("invalid_target", "resource does not name this server")

        return await self._issue_tokens(
            client_id=client.client_id,
            user_sub=record.user_sub,
            provider=record.provider,
            scope=record.scope,
            resource=record.resource,
            now=now,
        )

    async def _refresh_token_grant(
        self, client: OAuthClient, form: dict[str, str], now: float
    ) -> dict[str, Any]:
        presented = (form.get("refresh_token") or "").strip()
        if not presented:
            raise OAuthError("invalid_request", "refresh_token is missing")
        record = await self.store.get_token(hash_secret(presented), "refresh", now=now)
        if record is None:
            raise OAuthError(
                "invalid_grant", "that refresh token is unknown, revoked or expired"
            )
        if record.client_id != client.client_id:
            raise OAuthError(
                "invalid_grant", "that refresh token belongs to another client"
            )
        scope = (form.get("scope") or record.scope).strip() or record.scope
        if scope != record.scope:
            # RFC 6749 §6: a refresh may narrow scope, never widen it. With
            # one scope there is nothing to narrow to, so anything different
            # is a request for something that was not granted.
            raise OAuthError("invalid_scope", "a refresh cannot change the scope")
        issued = await self._issue_tokens(
            client_id=client.client_id,
            user_sub=record.user_sub,
            provider=record.provider,
            scope=record.scope,
            resource=record.resource,
            now=now,
        )
        # Rotation: the presented refresh token dies here. Deleted after the
        # new pair is written, so a crash in between leaves the user with a
        # token that still works rather than none at all.
        await self.store.revoke_token(record.token_hash)
        return issued

    async def revoke(self, form: dict[str, str], authorization: str | None = None) -> None:
        """RFC 7009. Always succeeds from the client's point of view.

        The RFC is explicit (§2.2): an invalid or already-revoked token gets
        200, because telling a caller which tokens exist is an oracle and
        "the token is not valid" is the outcome they asked for anyway.
        """
        try:
            await self._authenticate_client(form, authorization)
        except OAuthError:
            # A revoke with bad client credentials still must not tell the
            # caller anything. Nothing is revoked; nothing is disclosed.
            return
        token = (form.get("token") or "").strip()
        if not token:
            return
        try:
            await self.store.revoke_token(hash_secret(token))
        except OAuthStoreError as exc:
            logger.warning("revocation failed: %s", exc)

    # ── the resource server ──────────────────────────────────────────────

    async def validate_access_token(
        self, token: str, now: float | None = None
    ) -> TokenRecord | None:
        if not token or not token.startswith(ACCESS_TOKEN_PREFIX):
            return None
        try:
            return await self.store.get_token(hash_secret(token), "access", now=now)
        except OAuthStoreError as exc:
            # A database outage is not "your token is bad", but there is no
            # way to serve the request without the lookup, so the caller gets
            # the same 401 and we get the log line.
            logger.warning("access token lookup failed: %s", exc)
            return None

    def challenge_header(self, error: str = "", description: str = "") -> str:
        parts = [f'Bearer resource_metadata="{self.resource_metadata_url}"']
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return ", ".join(parts)


def _redirect_uri_allowed(uri: str) -> bool:
    """What a registered redirect_uri may look like.

    https anywhere; http ONLY on loopback (RFC 8252 §7.3 -- this is how a
    desktop MCP client receives its code); and private-use schemes like
    `cursor://` or `vscode://`, which is how the other half of them do it.
    Plain http to a remote host is refused: a code delivered over cleartext
    to somewhere we cannot see is the one shape that is always a mistake.
    """
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http":
        host = (parts.hostname or "").lower()
        return host in ("127.0.0.1", "::1", "localhost")
    # A private-use scheme must be a real scheme, not a bare path.
    return bool(parts.scheme) and ":" in uri and not uri.startswith(("javascript:", "data:"))


def build_oauth_support(
    product: str, origin: str, connect: Any
) -> "OAuthSupport | None":
    """The MCP-protocol OAuth feature for one product, or None.

    Requires everything `/connect` requires (it reuses that sign-in and that
    key store) plus nothing else -- so on a deployment where day 1 is
    configured, day 2 comes on with it. `MCP_OAUTH=off` turns it off without
    touching day 1, which is the rollback that needs no code change.

    Never raises. A deployment where this returns None serves `/mcp` exactly
    as it does today and answers 404 on `/mcp/oauth`, which is the honest
    answer: there is nothing behind it.
    """
    if connect is None:
        return None
    if os.environ.get("MCP_OAUTH", "").strip().lower() in {"0", "off", "false", "no"}:
        logger.info("MCP_OAUTH is off; /mcp/oauth stays unregistered.")
        return None
    store = build_oauth_store()
    if not getattr(store, "available", False):
        return None
    return OAuthSupport(
        store=store,
        auth=connect.auth,
        origin=origin.rstrip("/"),
        product=product,
    )


# ── the /mcp/oauth gate ──────────────────────────────────────────────────


def strip_identity_headers(scope: dict) -> dict:
    """Remove any inbound copy of the headers the gate injects.

    Unconditional, on every request, whether or not OAuth is configured and
    whatever the path. This is the half of the mechanism that makes the
    injected header trustworthy: `_resolve` in server.py serves a stored
    RapidAPI key on the strength of it, so a caller who could set it himself
    would be able to spend a stranger's plan.
    """
    headers = scope.get("headers") or []
    kept = [
        (name, value)
        for name, value in headers
        if name.decode("latin-1").lower() not in IDENTITY_HEADERS
    ]
    if len(kept) == len(headers):
        return scope
    scope = dict(scope)
    scope["headers"] = kept
    return scope


def _json_response(status: int, body: dict[str, Any], extra: dict[str, str] | None = None):
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    for name, value in (extra or {}).items():
        headers.append((name.encode("ascii"), value.encode("latin-1")))
    return payload, headers


class OAuthResourceGate:
    """ASGI wrapper: strip injected headers everywhere, guard /mcp/oauth.

    Sits above the host dispatcher rather than inside a product app, because
    the strip has to happen before ANY route sees a request and the guard has
    to know which product's tokens to check -- both of which are properties
    of the process, not of one FastMCP instance.
    """

    def __init__(self, app, support_for_scope) -> None:
        self.app = app
        self.support_for_scope = support_for_scope

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        scope = strip_identity_headers(scope)
        path = scope.get("path", "")
        if path.rstrip("/") != MCP_OAUTH_PATH:
            await self.app(scope, receive, send)
            return

        support = self.support_for_scope(scope)
        if support is None:
            payload, headers = _json_response(
                404,
                {
                    "error": "not_found",
                    "error_description": (
                        "This deployment does not have MCP OAuth configured. "
                        "Use /mcp with a RapidAPI key."
                    ),
                },
            )
            await _send(send, 404, headers, payload)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }
        token = bearer_token(headers.get("authorization"))
        if not token:
            payload, out = _json_response(
                401,
                {
                    "error": "invalid_request",
                    "error_description": (
                        "This endpoint requires an OAuth access token. Your "
                        "MCP client should sign you in; if it cannot, use "
                        f"{support.issuer}/mcp with a RapidAPI key instead."
                    ),
                },
                {"WWW-Authenticate": support.challenge_header()},
            )
            await _send(send, 401, out, payload)
            return

        record = await support.validate_access_token(token)
        if record is None:
            payload, out = _json_response(
                401,
                {
                    "error": "invalid_token",
                    "error_description": (
                        "That access token is unknown, expired or revoked. "
                        "Sign in again."
                    ),
                },
                {
                    "WWW-Authenticate": support.challenge_header(
                        "invalid_token", "the access token is expired or revoked"
                    )
                },
            )
            await _send(send, 401, out, payload)
            return

        # Authenticated. Rewrite to the real MCP route and hand the identity
        # to the tool layer through headers it can read from the live
        # request, exactly as it reads a RapidAPI key today.
        scope = dict(scope)
        scope["path"] = MCP_PATH
        scope["raw_path"] = MCP_PATH.encode("ascii")
        scope["headers"] = list(scope.get("headers") or []) + [
            (SUBJECT_HEADER.encode("ascii"), header_safe(record.user_sub).encode("ascii")),
            (PROVIDER_HEADER.encode("ascii"), header_safe(record.provider).encode("ascii")),
            (CLIENT_HEADER.encode("ascii"), header_safe(record.client_id).encode("ascii")),
        ]
        await self.app(scope, receive, send)


async def _send(send, status: int, headers: list, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ── the consent page ─────────────────────────────────────────────────────


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def consent_html(
    *,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    email: str,
    product: str,
    sealed: str,
    csrf: str,
    has_key: bool,
    connect_url: str,
) -> str:
    """What the human sees before a client gets a token.

    Names the client, the account, and what the grant actually costs -- which
    on this server is real money on the user's own RapidAPI plan, so it is
    said in those words rather than as "access your data".
    """
    from .connect import _EXTRA_STYLE  # local: keeps the import graph flat

    what = (
        "search live flight fares"
        if product == "flights"
        else "search live hotel rates"
        if product == "hotels"
        else "search live flight fares and hotel rates"
    )
    key_block = (
        '<p class="good">Your RapidAPI key is connected. Searches this client '
        "runs will be billed to your own plan.</p>"
        if has_key
        else (
            '<p class="bad">You have not connected a RapidAPI key yet. You can '
            "approve this client now, but its searches will come back asking "
            f'you to connect one at <a href="{_e(connect_url)}">{_e(connect_url)}</a>.</p>'
        )
    )
    host = _e(urlsplit(redirect_uri).netloc or redirect_uri)
    return f"""{_EXTRA_STYLE}
<h1>Connect {_e(client_name)}?</h1>
<p class="note">Signed in as <strong>{_e(email or "your Google account")}</strong>.</p>
<div class="card">
<p><strong>{_e(client_name)}</strong> is asking to {_e(what)} on your behalf
through FlightPowers.</p>
<ul>
  <li>It will be able to run searches billed to
      <strong>your own RapidAPI plan</strong>. Nothing else.</li>
  <li>It never sees your RapidAPI key. It gets a token that points at the key
      you stored here, and that token stops working the moment you disconnect.</li>
  <li>Access lasts one hour at a time and is renewed silently until you
      revoke it.</li>
</ul>
{key_block}
<p class="note">It will be sent back to <code>{host}</code>.</p>
</div>
<form method="post" action="{_e(AUTHORIZE_PATH)}">
  <input type="hidden" name="csrf" value="{_e(csrf)}">
  <input type="hidden" name="request" value="{_e(sealed)}">
  <p>
    <button class="btn" type="submit" name="decision" value="approve">Approve</button>
    <button class="btn danger" type="submit" name="decision" value="deny">Deny</button>
  </p>
</form>
<p class="note">Client id <code>{_e(client_id)}</code>.
<a href="/connect">Manage your key</a> &middot;
<a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a></p>
"""


def error_html(title: str, message: str) -> str:
    from .connect import _EXTRA_STYLE  # local: keeps the import graph flat

    return (
        _EXTRA_STYLE
        + f"<h1>{_e(title)}</h1>"
        + f'<p class="bad">{_e(message)}</p>'
        + '<p class="note">Nothing was approved and nothing was changed. '
        'Start the sign-in again from your MCP client, or '
        '<a href="/connect">manage your key</a>.</p>'
    )


def redirect_with(base: str, params: dict[str, str]) -> str:
    """Append parameters to a redirect_uri, preserving any it already has."""
    clean = {k: v for k, v in params.items() if v}
    if not clean:
        return base
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{urlencode(clean)}"


def new_state() -> str:
    return secrets.token_urlsafe(16)
