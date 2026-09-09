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
import ipaddress
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

from . import cimd
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
from .ratelimit import UNKNOWN_IP
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

# ── registration hygiene (day 3) ─────────────────────────────────────────
# Registration is open, because the MCP spec requires it and because a
# client_id on its own authorises nothing. Open is not the same as unlimited:
# anyone who can POST can make a row, and rows nobody cleans up are how a
# small table becomes an incident. Three mechanisms, deliberately different
# in kind:
#
#   * a per-instance RATE limit (src/ratelimit.py) -- cheap, spoofable, first;
#   * these DURABLE per-day caps, counted in Postgres, so every instance
#     agrees on the number;
#   * a SWEEP that deletes registrations which never became an
#     authorization, so the caps are counted against a table that does not
#     silently fill with abandoned rows.
#
#: Registrations from one address in a rolling day. Generous: a NAT, a CI
#: runner or one directory registering on behalf of many users all share an
#: address, and refusing those is a worse failure than the one being
#: prevented.
DCR_MAX_PER_IP_PER_DAY = 30
#: Registrations from everyone in a rolling day. A real day on this server is
#: single digits, so this number looks absurd -- and that is the point. A
#: global cap set anywhere near real traffic is not a defence, it is a lever:
#: whoever can vary the address the per-address cap is keyed on walks the
#: global counter up in minutes, and from then on every legitimate
#: `/oauth/register` -- a new Claude, Cursor or Smithery user -- gets 429 for
#: a day. The cheap defence would have become a denial of service with a
#: 24-hour tail. So the per-address cap stays as the one that bites, this one
#: is only a backstop against a table growing without bound, and the number
#: that gets a human's attention is the WARN threshold below, which logs and
#: refuses nothing.
DCR_MAX_PER_DAY = 5_000
#: Registrations in a rolling day that mean "look at this". Not a refusal:
#: crossing it logs, once per registration past the line, and that is all.
DCR_WARN_PER_DAY = 500
#: A registration that no human has approved within this long is litter.
#: Seven days, not one: MCP clients commonly register when they are installed
#: and are authorized whenever the person next opens the app, and a sweep
#: that runs a day after registration deletes rows that were about to be
#: used. The row is a name and a redirect URI; keeping it a week costs
#: nothing next to signing somebody out mid-flow.
STALE_CLIENT_SECONDS = 7 * 24 * 3600
#: How often one instance will spend two DELETEs on housekeeping.
SWEEP_INTERVAL_SECONDS = 15 * 60


#: When this instance last swept. Module-level because it is a property of
#: the process, not of one product's OAuthSupport -- both products share one
#: database and sweeping it twice is wasted work.
_SWEEP_STATE = {"last": 0.0}


def reset_sweep_clock() -> None:
    """Make the next registration sweep. For tests and for a fresh process."""
    _SWEEP_STATE["last"] = 0.0


# ── the refresh-retry grace window ───────────────────────────────────────
# Rotation plus reuse detection is the right shape (OAuth 2.1 §4.14.2) and it
# has one ugly edge: a client whose refresh response never arrived retries
# the token it still has, we see a rotated token coming back, and the whole
# family dies. The user is signed out by a dropped packet.
#
# So the FIRST replay of the token we just rotated, within a few seconds and
# from the same client, is answered with the pair that request already
# produced -- an idempotent retry, not a new grant. It creates no token, and
# it is a race an attacker cannot rely on: they would have to present a
# stolen refresh token inside the same ten seconds as the honest client's
# retry, and the honest client's pair is what they would get. Anything later,
# or a second replay, is the real thing and still kills the family.
#
#: How long a rotated refresh token may come back and be answered instead of
#: revoked.
REFRESH_REPLAY_GRACE_SECONDS = 10
#: Per PROCESS, and deliberately not in the database: this is a nicety for a
#: retry that happens milliseconds later, and a Vercel instance that does not
#: have the entry simply falls through to reuse detection, which is the
#: conservative answer. Nothing is weakened by a miss.
_REPLAY_MAX = 512
_REPLAY: dict[str, tuple[float, str, dict[str, Any]]] = {}


def reset_replay_grace() -> None:
    """Forget every in-flight retry. For tests and for a fresh process."""
    _REPLAY.clear()


def _remember_rotation(
    token_hash: str, client_id: str, issued: dict[str, Any], now: float
) -> None:
    if len(_REPLAY) >= _REPLAY_MAX:
        for key, (deadline, _, _) in list(_REPLAY.items()):
            if deadline <= now:
                del _REPLAY[key]
        if len(_REPLAY) >= _REPLAY_MAX:
            # A dictionary that grows without limit is its own denial of
            # service, and losing the grace window only costs a retry.
            _REPLAY.clear()
    _REPLAY[token_hash] = (now + REFRESH_REPLAY_GRACE_SECONDS, client_id, issued)


def _take_replay(
    token_hash: str, client_id: str, now: float
) -> dict[str, Any] | None:
    """The pair this token already produced, once, inside the window."""
    entry = _REPLAY.pop(token_hash, None)  # popped: once, whatever happens
    if entry is None:
        return None
    deadline, owner, issued = entry
    if now >= deadline or not hmac.compare_digest(owner, client_id):
        return None
    return issued


def _cap(name: str, default: int) -> int:
    """A registration cap, overridable by env so ops can loosen one without
    a deploy. A malformed value keeps the default rather than turning the cap
    off, because "0" and "not a number" must not mean "unlimited"."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; keeping %d", name, raw, default)
        return default
    return value if value > 0 else default

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
#: The address the token was approved by, so a signed-in caller with no key
#: can be told which account they are signed in as. Stripped from inbound
#: requests with the rest: it is displayed back to a user, and a value a
#: caller could set is a value a caller could use to make our own reply lie.
EMAIL_HEADER = "x-fp-oauth-email"
IDENTITY_HEADERS = (SUBJECT_HEADER, PROVIDER_HEADER, CLIENT_HEADER, EMAIL_HEADER)


class OAuthError(Exception):
    """One OAuth 2.0 error, with the code the RFC names.

    `redirectable` says whether the client's `redirect_uri` has been
    validated yet. It has not for a bad `client_id` or a bad `redirect_uri`,
    and RFC 6749 §4.1.2.1 is explicit that those two must NOT be redirected --
    doing so turns the authorization endpoint into an open redirector.
    """

    def __init__(
        self, code: str, description: str = "", *, redirectable: bool = True,
        status: int = 400, retry_after: int = 0,
    ) -> None:
        super().__init__(description or code)
        self.code = code
        self.description = description
        self.redirectable = redirectable
        self.status = status
        #: Seconds, for a `Retry-After` header. Only a refusal that a caller
        #: can usefully repeat later sets it.
        self.retry_after = retry_after

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
            # A client_id that is an https URL we can fetch, instead of one
            # we minted. Smithery asks for this before it will proxy a remote
            # OAuth server, and it is how a client avoids leaving a row in
            # our table per install. DCR is still offered above; this is an
            # additional shape, not a replacement. See src/cimd.py.
            "client_id_metadata_document_supported": True,
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

    # ── the client behind a client_id ─────────────────────────────────────

    async def lookup_client(
        self, client_id: str, redirect_uri: str = ""
    ) -> OAuthClient | None:
        """The client for this `client_id`, from our table or from its URL.

        Two shapes, told apart by the id itself: `fpcl_…` is a row we wrote
        at registration, an https URL is a Client ID Metadata Document we
        fetch and validate now (src/cimd.py). Nothing else is accepted, so a
        client that registered the old way keeps behaving exactly as it did.

        `redirect_uri` is checked against the DOCUMENT here rather than left
        to the caller, because for a CIMD client the document is the only
        registration there is: skipping it would let anyone who knows a CIMD
        URL have that client's codes delivered somewhere else.
        """
        if not cimd.is_cimd_client_id(client_id):
            return await self.store.get_client(client_id)
        try:
            document = await cimd.load(client_id)
            uris = cimd.redirect_uris(document)
        except cimd.CimdError as exc:
            logger.info("CIMD client_id %s refused: %s", client_id, exc)
            raise OAuthError(
                "invalid_client", str(exc), redirectable=False, status=400
            ) from exc
        allowed = tuple(u for u in uris if _redirect_uri_allowed(u))
        if not allowed:
            raise OAuthError(
                "invalid_client",
                "the client metadata document lists no usable redirect_uris",
                redirectable=False,
            )
        if redirect_uri and redirect_uri not in allowed:
            raise OAuthError(
                "invalid_request",
                "that redirect_uri is not listed in the client metadata document",
                redirectable=False,
            )
        return OAuthClient(
            client_id=client_id,
            client_name=cimd.client_name(document, client_id),
            redirect_uris=allowed,
            token_endpoint_auth_method="none",
            scope=str(document.get("scope") or DEFAULT_SCOPE),
            client_secret_hash="",
            created_at=time.time(),
            metadata={},
            ephemeral=True,
        )

    # ── dynamic client registration ──────────────────────────────────────

    async def _sweep(self, now: float) -> None:
        """Housekeeping, at most once every SWEEP_INTERVAL per instance.

        Hung off registration rather than a cron because this deployment has
        no scheduler and adding one for two DELETEs would be the bigger
        change. Registration is the only endpoint that GROWS the tables, so
        it is the honest place to pay for cleaning them.

        Best effort throughout: a sweep that fails must never turn a valid
        registration into an error.
        """
        if now - _SWEEP_STATE["last"] < SWEEP_INTERVAL_SECONDS:
            return
        # Stamped BEFORE the work, so a store that is failing does not get a
        # sweep attempt per registration.
        _SWEEP_STATE["last"] = now
        try:
            expired = await self.store.purge_expired(now)
            stale = await self.store.purge_stale_clients(now - STALE_CLIENT_SECONDS)
        except OAuthStoreError as exc:
            logger.warning("OAuth sweep failed: %s", exc)
            return
        if expired or stale:
            logger.info(
                "OAuth sweep removed %d expired code(s)/token(s) and %d "
                "unused client registration(s)",
                expired,
                stale,
            )

    async def _check_registration_caps(self, ip: str, now: float) -> None:
        """The durable half of the registration limit.

        Counted in Postgres, so every Vercel instance sees the same number --
        unlike the per-instance rate limiter, which is the cheap first line.
        Both are needed: the rate limiter stops a burst, this stops a slow
        drip that would otherwise fill the table over a day.
        """
        since = now - 24 * 3600
        per_day = _cap("MCP_OAUTH_DCR_MAX_PER_DAY", DCR_MAX_PER_DAY)
        per_ip = _cap("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", DCR_MAX_PER_IP_PER_DAY)
        warn_per_day = _cap("MCP_OAUTH_DCR_WARN_PER_DAY", DCR_WARN_PER_DAY)
        # `unknown` is the shared bucket for callers whose address the
        # platform did not give us (`ratelimit.client_ip`). Counting a
        # durable per-address cap against it would mean one missing header on
        # the edge locks every registration on the server out for a day, so
        # it is treated as "no address": the rate limiter still buckets them
        # together, and the global backstop still applies.
        if ip and ip != UNKNOWN_IP:
            from_here = await self.store.count_clients_since(since, ip)
            if from_here >= per_ip:
                logger.warning(
                    "registration cap: %s has registered %d client(s) today",
                    ip,
                    from_here,
                )
                raise OAuthError(
                    "temporarily_unavailable",
                    "too many client registrations from this address today; "
                    "try again later",
                    status=429,
                    retry_after=3600,
                )
        total = await self.store.count_clients_since(since)
        if warn_per_day <= total < per_day:
            logger.warning(
                "registration volume: %d client registration(s) in the last "
                "24h, above the %d that a normal day looks like",
                total,
                warn_per_day,
            )
        if total >= per_day:
            logger.warning("registration cap: %d registrations today", total)
            raise OAuthError(
                "temporarily_unavailable",
                "this server is not accepting new client registrations right "
                "now; try again later",
                status=429,
                retry_after=3600,
            )

    async def register(
        self, body: dict[str, Any], ip: str = "", now: float | None = None
    ) -> dict[str, Any]:
        """RFC 7591. Returns the registration response to send back.

        Open registration, which the MCP spec requires and which is safe
        here for one reason worth stating: a `client_id` authorises nothing.
        Every flow through it still ends at a consent page that a human has
        to be signed into Google to see and has to press a button on. The
        row is a name and a redirect URI, not a permission.

        Open, capped and swept: see `_check_registration_caps` and `_sweep`.
        """
        now = now if now is not None else time.time()
        await self._sweep(now)
        await self._check_registration_caps(ip, now)
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
            created_at=now,
            registered_ip=(ip or "")[:64],
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
            client = await self.lookup_client(
                client_id, (params.get("redirect_uri") or "").strip()
            )
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
        self,
        request: dict[str, str],
        sub: str,
        now: float | None = None,
        email: str = "",
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
                user_email=email,
            )
        )
        # This registration has now been approved by a human, so the sweep
        # must never take it. A CIMD client has no row to stamp.
        if not cimd.is_cimd_client_id(request["client_id"]):
            try:
                await self.store.mark_client_authorized(request["client_id"], now)
            except OAuthStoreError as exc:
                # Bookkeeping, not the grant. The NOT EXISTS clauses in the
                # sweep already protect a client that has a code or a token.
                logger.warning("could not stamp client as authorized: %s", exc)
        return code

    async def note_consent_shown(
        self, client_id: str, now: float | None = None
    ) -> None:
        """A consent page for this client is being put in front of a human.

        Same stamp the approval writes, moved earlier for one reason: the
        sweep deletes registrations with no code, no token and no stamp, and
        until this existed the only stamp happened when Approve was pressed.
        A client that registered at install and signs in a week later spent
        the whole consent page inside a window where the sweep could take its
        row -- and the exchange that followed would fail `invalid_client`
        with nothing in the logs naming the cause.

        Best effort, like the stamp in `issue_code`: this is bookkeeping, and
        a store hiccup must not stop a page rendering. A CIMD client has no
        row to stamp.
        """
        if not client_id or cimd.is_cimd_client_id(client_id):
            return
        try:
            await self.store.mark_client_authorized(
                client_id, now if now is not None else time.time()
            )
        except OAuthStoreError as exc:
            logger.warning("could not stamp client at the consent page: %s", exc)

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        user_sub: str,
        provider: str,
        scope: str,
        resource: str,
        now: float,
        email: str = "",
        family_id: str = "",
    ) -> dict[str, Any]:
        access = mint(ACCESS_TOKEN_PREFIX)
        refresh = mint(REFRESH_TOKEN_PREFIX)
        # One family per authorization, carried across every rotation. It is
        # what makes "revoke the whole line" a single statement when a
        # rotated refresh token comes back.
        family = family_id or new_family()
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
                user_email=email,
                family_id=family,
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
                user_email=email,
                family_id=family,
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
            client = await self.lookup_client(client_id)
        except OAuthError as exc:
            # A CIMD document that stopped resolving between authorize and
            # the exchange. `invalid_client` with the document's reason, at
            # 401 like every other client-authentication failure here.
            raise OAuthError(
                "invalid_client", exc.description or exc.code, status=401
            ) from exc
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
            email=record.user_email,
        )

    async def _refresh_token_grant(
        self, client: OAuthClient, form: dict[str, str], now: float
    ) -> dict[str, Any]:
        presented = (form.get("refresh_token") or "").strip()
        if not presented:
            raise OAuthError("invalid_request", "refresh_token is missing")
        presented_hash = hash_secret(presented)
        record = await self.store.get_token(presented_hash, "refresh", now=now)
        if record is None:
            # Before assuming theft: the same client asking again, seconds
            # after we rotated this token, is a retry of a response it never
            # received. Answer it with the pair that request produced. See
            # REFRESH_REPLAY_GRACE_SECONDS.
            retry = _take_replay(presented_hash, client.client_id, now)
            if retry is not None:
                logger.info(
                    "refresh retry inside the grace window for client %s; "
                    "returning the pair that rotation already issued",
                    client.client_id,
                )
                return retry
            await self._detect_reuse(presented_hash, client, now)
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
            email=record.user_email,
            family_id=record.family_id,
        )
        # Rotation: the presented refresh token stops working here. Rotated
        # after the new pair is written, so a crash in between leaves the
        # user with a token that still works rather than none at all.
        #
        # STAMPED, not deleted (day 3). A deleted row and a token that never
        # existed are indistinguishable, and the difference is the whole
        # signal: a rotated refresh token coming back means either a client
        # that lost the response or a copy in somebody else's hands, and
        # OAuth 2.1 §4.14.2 says to assume the second.
        await self.store.rotate_token(record.token_hash, now)
        _remember_rotation(record.token_hash, client.client_id, issued, now)
        return issued

    async def _detect_reuse(
        self, token_hash: str, client: OAuthClient, now: float
    ) -> None:
        """A refresh token that was already rotated, presented again.

        The response is the same `invalid_grant` either way -- this is about
        what happens to the OTHER tokens. Every token descended from that one
        authorization is deleted, so an attacker replaying a stolen refresh
        token cannot keep the access token they got with it, and the real
        user's next call fails in a way that makes them sign in again.

        The honest retry -- a client asking again for a response it never
        received -- is caught before this function runs, by the few-second
        grace window in `_refresh_token_grant`. What reaches here is a
        rotated token coming back late, or coming back twice, and there the
        safe reading is theft: a stolen refresh token is indistinguishable
        from a retried one, and OAuth 2.1 §4.14.2 says to assume the first.
        """
        try:
            stale = await self.store.get_token_any(token_hash, "refresh")
        except OAuthStoreError as exc:
            logger.warning("reuse check could not read the store: %s", exc)
            return
        if stale is None or stale.revoked_at is None:
            return
        logger.warning(
            "refresh token reuse detected for client %s (sub=%s); revoking "
            "the whole token family",
            client.client_id,
            stale.user_sub,
        )
        try:
            dropped = await self.store.revoke_family(stale.family_id)
        except OAuthStoreError as exc:
            logger.warning("could not revoke the token family: %s", exc)
            return
        if dropped:
            logger.warning("revoked %d token(s) after refresh reuse", dropped)

    async def revoke(self, form: dict[str, str], authorization: str | None = None) -> None:
        """RFC 7009. Always succeeds from the client's point of view.

        The RFC is explicit (§2.2): an invalid or already-revoked token gets
        200, because telling a caller which tokens exist is an oracle and
        "the token is not valid" is the outcome they asked for anyway.
        """
        try:
            client = await self._authenticate_client(form, authorization)
        except OAuthError:
            # A revoke with bad client credentials still must not tell the
            # caller anything. Nothing is revoked; nothing is disclosed.
            return
        token = (form.get("token") or "").strip()
        if not token:
            return
        token_hash = hash_secret(token)
        kind = "refresh" if token.startswith(REFRESH_TOKEN_PREFIX) else "access"
        try:
            # RFC 7009 §2.1: the server "validates whether the token was
            # issued to the client making the revocation request". Without
            # that check, any client holding somebody else's token can sign
            # that user out -- and since revoking a refresh token now takes
            # the whole family, the blast radius is a whole authorization
            # rather than one token. The answer stays 200 either way: §2.2's
            # silence rule does not stop applying because we said no.
            record = await self.store.get_token_any(token_hash, kind)
            if record is not None and record.client_id != client.client_id:
                logger.warning(
                    "client %s tried to revoke a token issued to %s",
                    client.client_id,
                    record.client_id,
                )
                return
            # Revoking a refresh token SHOULD revoke the access tokens issued
            # with it. The family id is exactly that set, so one statement
            # does it -- and it also means a client that logs out cannot
            # leave a live access token behind.
            family = (
                record.family_id
                if record is not None and kind == "refresh"
                else ""
            )
            if family:
                await self.store.revoke_family(family)
            else:
                await self.store.revoke_token(token_hash)
        except OAuthStoreError as exc:
            logger.warning("revocation failed: %s", exc)

    # ── the resource server ──────────────────────────────────────────────

    async def validate_access_token(
        self, token: str, now: float | None = None
    ) -> TokenRecord | None:
        if not token or not token.startswith(ACCESS_TOKEN_PREFIX):
            return None
        try:
            record = await self.store.get_token(hash_secret(token), "access", now=now)
        except OAuthStoreError as exc:
            # A database outage is not "your token is bad", but there is no
            # way to serve the request without the lookup, so the caller gets
            # the same 401 and we get the log line.
            logger.warning("access token lookup failed: %s", exc)
            return None
        if record is None:
            return None
        # AUDIENCE. Both products share one deployment, one database and one
        # stored RapidAPI key per user, so a token approved on the flights
        # consent page ("search live flight fares") would otherwise be
        # accepted on the hotels hostname and spend the user's hotels
        # subscription -- a grant the user was never shown. MCP 2025-06-18
        # requires a resource server to check that a token was issued for it,
        # and RFC 8707 exists for exactly this confused-deputy case. The
        # `resource` column was already written down for this; this is where
        # it is read. An empty value can only come from a row predating the
        # column's default and is treated as unscoped, which is what
        # `resource_matches` already means by "".
        if not self.resource_matches(record.resource):
            logger.warning(
                "access token for %s presented at %s; refused",
                record.resource,
                self.resource_url,
            )
            return None
        return record

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
            (EMAIL_HEADER.encode("ascii"), header_safe(record.user_email).encode("ascii")),
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


def is_loopback_redirect(uri: str) -> bool:
    """True when this `redirect_uri` can only be a server on the user's own
    machine.

    RFC 8252 §7.3: a native app receives its authorization response on
    `http://127.0.0.1:<port>` (or `[::1]`), on an ephemeral port it opened
    for the occasion. That listener exists while the client is waiting for
    an answer and not a moment longer, and for a client that registered by
    hand it may never have existed at all -- MCP Inspector's own probe
    registration uses `http://127.0.0.1:9999/cb`.

    So a loopback address is the one case where we can say, from the URL
    alone and without touching the network, that the redirect stands a good
    chance of landing the user on the browser's own connection-error page.
    That is the judgement this function encodes, and it is deliberately the
    ONLY case: a remote `https://` callback might be down too, but we cannot
    know that without a request we have no business making (it would be an
    outbound fetch to an address a stranger registered), and a working
    redirect is a better outcome than a page saying "we did not try".
    """
    host = (urlsplit(uri).hostname or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # `0.0.0.0` is unspecified rather than loopback, and a client that
    # registered it is a client whose callback is just as unreachable.
    return address.is_loopback or address.is_unspecified


def cancelled_html(target: str, *, title: str, lead: str) -> str:
    """Our own end of the road for a flow that ended without a token.

    Shown instead of bouncing the browser at a loopback callback. It says
    the one thing the user needs (nothing was stored) and still offers the
    RFC-conformant redirect as a link, because a client that IS listening
    should get its `error=access_denied` and stop spinning -- the change
    here is that the redirect stops being a thing that happens TO the user
    and becomes a thing they can choose.
    """
    from .connect import _EXTRA_STYLE  # local: keeps the import graph flat

    # `target` is the full RFC redirect, error parameters and all: the link
    # below has to carry them or a client that follows it learns nothing.
    host = _e(urlsplit(target).netloc or target)
    return (
        _EXTRA_STYLE
        + f"<h1>{_e(title)}</h1>"
        + f"<p>{_e(lead)}</p>"
        + '<div class="card"><p>Nothing was stored and nothing was shared. '
        "Your RapidAPI key, if you had already connected one, is exactly as "
        "it was.</p>"
        "<p>You can close this tab.</p></div>"
        f'<p class="note">Your client was waiting at <code>{host}</code>. '
        f'<a href="{_e(target)}">Tell it you cancelled</a> if it is '
        "still open, or just close it and try again from the client.</p>"
        '<p class="note"><a href="/connect">Manage your key</a> &middot; '
        '<a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a></p>'
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


def new_family() -> str:
    """The id every token descended from one authorization shares."""
    return "fam_" + secrets.token_urlsafe(16)
