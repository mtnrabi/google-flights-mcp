"""
Google sign-in for the /connect page, and the signed tokens it hands out.

Two different things are signed here and they are not interchangeable:

* a **session cookie**, minutes-to-an-hour long, that says "this browser is
  Google account <sub>". It exists only so /connect can show a page and
  accept a form post. It is never accepted on /mcp.
* a **connect token** (`fpk_…`), long-lived and revocable, that the user
  pastes into their MCP client instead of their RapidAPI key. It says
  "resolve the key stored for <sub>", nothing more: on its own it authorises
  no spend, because the row it points at is what actually holds a key and
  Disconnect deletes that row.

Both are HMAC-SHA256 over a compact JSON payload, keyed by a value derived
from MCP_KEY_MASTER. Deriving rather than adding a fourth secret is
deliberate -- one secret to set, one secret to rotate, and a rotation
invalidates sessions and connect tokens at the same moment it stops the
stored ciphertext decrypting, which is the behaviour you want: after a
rotation everybody reconnects, and nobody is left holding a token that
resolves to a key nobody can read.

Why not fastmcp's GoogleProvider here
-------------------------------------
`fastmcp.server.auth.providers.google.GoogleProvider` is the right thing for
MCP-protocol OAuth (a client doing DCR + PKCE against us). It is NOT the
right thing for this page, for two reasons that are both about production:

1. Passing `auth=` to `FastMCP` wraps `/mcp` in `RequireAuthMiddleware`, so
   every existing caller -- all of whom authenticate with a RapidAPI key and
   no bearer token -- starts getting 401. Auth here has to be optional or it
   is an outage.
2. `OAuthProxy` keeps dynamically-registered clients in an `AsyncKeyValue`
   that defaults to process memory. On Vercel, instances come and go and a
   request can land on one that never saw the registration, so a client that
   registered a second ago is told its client_id is unknown. Wiring it needs
   a shared store first (Upstash is already reachable from src/stores.py).

So the browser flow is a plain OAuth 2.0 authorization-code + PKCE exchange
against Google, ~150 lines, no new dependency, and the MCP-protocol provider
stays a follow-up with its blocker written down.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Minimum for "who is this". No Drive, no Calendar, no profile: the consent
#: screen a user sees should ask for exactly what the feature needs, because
#: a scope list longer than the feature is the reason people abandon a
#: sign-in they were otherwise happy with.
GOOGLE_SCOPES = ("openid", "email")

#: Marks a connect token on sight. Load-bearing: `authorization: Bearer …`
#: is ALSO one of the ways a RapidAPI key arrives (credentials.py), and
#: without a prefix that can only be ours there is no way to tell a connect
#: token from a key that happens to be 60 characters long -- we would forward
#: it to RapidAPI, get a 401, and tell the user their key was rejected.
CONNECT_TOKEN_PREFIX = "fpk_"

SESSION_COOKIE = "fp_session"
OAUTH_COOKIE = "fp_oauth"
COOKIE_PATH = "/connect"

SESSION_TTL_SECONDS = 60 * 60           # one hour of browser session
OAUTH_STATE_TTL_SECONDS = 10 * 60       # one round trip to Google
CONNECT_TOKEN_TTL_SECONDS = 90 * 24 * 3600


class WebAuthError(RuntimeError):
    """Anything that means "start the sign-in again"."""


# ── signing ──────────────────────────────────────────────────────────────


def derive_secret(master: bytes, label: bytes) -> bytes:
    """A per-purpose signing key from the master key.

    Separate labels so a session cookie can never be replayed as a connect
    token, or the other way round, even though both are HMAC-SHA256 over
    JSON. (A `typ` field in the payload is checked too; this is the belt to
    that pair of braces.)
    """
    return hmac.new(master, label, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def sign_payload(payload: dict[str, Any], secret: bytes, prefix: str = "") -> str:
    """`<prefix><b64 json>.<b64 hmac>` -- compact, URL-safe, no dependency."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{prefix}{body}.{_b64e(mac)}"


def verify_payload(
    token: str, secret: bytes, prefix: str = "", now: float | None = None
) -> dict[str, Any]:
    """The inverse. Raises WebAuthError for a bad prefix, a bad signature, a
    malformed payload or an expired one -- one exception type, because the
    only useful thing to tell a user about any of them is "sign in again".
    """
    if prefix:
        if not token.startswith(prefix):
            raise WebAuthError("token has the wrong prefix")
        token = token[len(prefix) :]
    body, _, mac = token.partition(".")
    if not body or not mac:
        raise WebAuthError("token is malformed")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _b64d(mac)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise WebAuthError("token is malformed") from exc
    # compare_digest, not ==: signature comparison is the one place in this
    # file where a timing difference is an oracle.
    if not hmac.compare_digest(expected, given):
        raise WebAuthError("token signature does not match")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebAuthError("token payload is malformed") from exc
    if not isinstance(payload, dict):
        raise WebAuthError("token payload is malformed")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise WebAuthError("token has no expiry")
    if (now if now is not None else time.time()) >= exp:
        raise WebAuthError("token has expired")
    return payload


def is_local_path(target: str) -> bool:
    """True for a value that can only be a path on this origin.

    An open redirect is the classic way a sign-in gets weaponised: the user
    authenticates on the real site and is then bounced to an attacker's copy.
    So the test is deliberately narrow -- one leading slash, never two (`//`
    is protocol-relative and goes to another host), no scheme, no backslash
    (some browsers normalise `\\` to `/`), and no control characters.
    """
    if not target or not isinstance(target, str):
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target or ":" in target.split("?", 1)[0]:
        return False
    return all(ch >= " " and ch != "\x7f" for ch in target)


# ── identity ─────────────────────────────────────────────────────────────


#: Values for `GoogleIdentity.flow` -- what this browser session was started
#: FOR, not what the account has. See `issue_session`.
FLOW_OAUTH = "oauth"    #: signed in on the way to an MCP client authorization
FLOW_DIRECT = ""        #: someone opened /connect themselves


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    #: Why this session exists. Carried in the session cookie so /connect can
    #: tell "I got here from Claude asking to connect" from "I typed the URL",
    #: and show a page that answers the question the visitor actually has.
    #: Empty for every session issued before this field existed, which is the
    #: right default: it means "direct visit", the behaviour that shipped.
    flow: str = FLOW_DIRECT


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Read the claims out of an id_token WITHOUT verifying its signature.

    That is correct here and only here: this id_token was just fetched by us,
    over TLS, directly from Google's token endpoint, in response to a code we
    generated -- OpenID Connect Core 3.1.3.7 says a client MAY skip
    verification when the token comes straight from the token endpoint over a
    protected channel. It would NOT be correct anywhere the token arrived
    from a client.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise WebAuthError("Google returned a malformed id_token")
    try:
        claims = json.loads(_b64d(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebAuthError("Google returned an unreadable id_token") from exc
    if not isinstance(claims, dict):
        raise WebAuthError("Google returned an unreadable id_token")
    return claims


@dataclass(frozen=True)
class GoogleWebAuth:
    """The browser half of Google sign-in for one origin."""

    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: bytes
    connect_secret: bytes

    # ── step 1: send them to Google ──────────────────────────────────────

    def start(self, next_path: str = "") -> tuple[str, str]:
        """Returns (authorize_url, signed state cookie value).

        PKCE even though this is a confidential client with a secret. It
        costs two lines and it closes the authorization-code interception
        window that a plain confidential flow leaves open on a redirect URI
        anybody can navigate to.

        `next_path` is where to land after the sign-in, and it rides INSIDE
        the signed state cookie rather than on the query string. That is what
        makes it safe: a value the browser cannot edit cannot be turned into
        an open redirect, so the callback can send the user straight on to a
        pending MCP authorization request without re-validating a URL a
        stranger supplied. `is_local_path` is still applied on the way out,
        because a bug that put a full URL in here should fail closed.
        """
        verifier = _b64e(os.urandom(32))
        challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(16)
        payload = {
            "typ": "oauth",
            "state": state,
            "v": verifier,
            "exp": int(time.time()) + OAUTH_STATE_TTL_SECONDS,
        }
        if next_path and is_local_path(next_path):
            payload["n"] = next_path
        cookie = sign_payload(payload, self.session_secret)
        url = f"{GOOGLE_AUTHORIZE_URL}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                # No refresh token is wanted: we never call Google again on
                # the user's behalf. Asking for offline access would mean
                # holding a credential we have no use for.
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return url, cookie

    # ── step 2: they come back ───────────────────────────────────────────

    async def finish(
        self,
        code: str,
        state: str,
        state_cookie: str,
        client: httpx.AsyncClient | None = None,
    ) -> GoogleIdentity:
        payload = verify_payload(state_cookie, self.session_secret)
        if payload.get("typ") != "oauth":
            raise WebAuthError("wrong token type")
        if not hmac.compare_digest(str(payload.get("state", "")), state):
            raise WebAuthError("state does not match")
        verifier = str(payload.get("v", ""))
        if not verifier:
            raise WebAuthError("state carried no PKCE verifier")

        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.post(GOOGLE_TOKEN_URL, data=data)
        finally:
            if own_client:
                await client.aclose()
        if response.status_code != 200:
            # Google's body echoes the code and sometimes the client_id;
            # neither belongs in a log line, so only the status is kept.
            logger.warning("Google token exchange failed: %d", response.status_code)
            raise WebAuthError("Google would not exchange that sign-in")
        body = response.json()
        claims = _decode_id_token_claims(str(body.get("id_token", "")))
        sub = str(claims.get("sub", "")).strip()
        if not sub:
            raise WebAuthError("Google returned no account id")
        if str(claims.get("aud", "")) != self.client_id:
            raise WebAuthError("Google returned a token for a different app")
        email = str(claims.get("email", "")).strip()
        if email and claims.get("email_verified") is False:
            # Keep the account, drop the address: an unverified email must
            # never end up somewhere it could be treated as a contact.
            email = ""
        return GoogleIdentity(sub=sub, email=email)

    def next_from_state(self, state_cookie: str | None) -> str:
        """Where the sign-in was headed, out of the signed state cookie.

        "" for anything that does not verify. Called by /connect/callback
        after `finish` has already accepted the same cookie, so a second
        signature check here costs one HMAC and removes the need for the two
        call sites to agree about which of them validated what.
        """
        if not state_cookie:
            return ""
        try:
            payload = verify_payload(state_cookie, self.session_secret)
        except WebAuthError:
            return ""
        if payload.get("typ") != "oauth":
            return ""
        target = str(payload.get("n", ""))
        return target if is_local_path(target) else ""

    # ── sessions and connect tokens ──────────────────────────────────────

    def issue_session(
        self,
        identity: GoogleIdentity,
        now: float | None = None,
        flow: str = FLOW_DIRECT,
    ) -> str:
        """A one-hour browser session for /connect.

        `flow` records what the sign-in was for. It is a UI hint and nothing
        else -- no route grants anything on the strength of it -- but it is
        signed with everything else here rather than put in a query
        parameter, because a hint the browser can edit is a hint that will be
        edited and then trusted by the next person who reads the code.
        """
        now = now if now is not None else time.time()
        payload = {
            "typ": "session",
            "sub": identity.sub,
            "email": identity.email,
            "exp": int(now) + SESSION_TTL_SECONDS,
        }
        if flow:
            payload["f"] = flow
        return sign_payload(payload, self.session_secret)

    def read_session(
        self, cookie: str | None, now: float | None = None
    ) -> GoogleIdentity | None:
        if not cookie:
            return None
        try:
            payload = verify_payload(cookie, self.session_secret, now=now)
        except WebAuthError:
            return None
        if payload.get("typ") != "session":
            return None
        sub = str(payload.get("sub", ""))
        if not sub:
            return None
        return GoogleIdentity(
            sub=sub,
            email=str(payload.get("email", "")),
            flow=str(payload.get("f", "") or FLOW_DIRECT),
        )

    def issue_connect_token(self, sub: str, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        return sign_payload(
            {
                "typ": "connect",
                "sub": sub,
                "exp": int(now) + CONNECT_TOKEN_TTL_SECONDS,
            },
            self.connect_secret,
            prefix=CONNECT_TOKEN_PREFIX,
        )

    def read_connect_token(self, token: str, now: float | None = None) -> str | None:
        """The Google `sub` a connect token names, or None.

        None rather than an exception on purpose: on the /mcp path this is
        one of several credential channels being tried, and a stale token
        must fall through to the header/query path instead of turning a
        working keyed request into an error.
        """
        if not token or not token.startswith(CONNECT_TOKEN_PREFIX):
            return None
        try:
            payload = verify_payload(
                token, self.connect_secret, prefix=CONNECT_TOKEN_PREFIX, now=now
            )
        except WebAuthError:
            return None
        if payload.get("typ") != "connect":
            return None
        sub = str(payload.get("sub", ""))
        return sub or None


def build_web_auth(
    origin: str,
    master: bytes,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> GoogleWebAuth | None:
    """The sign-in for one public origin, or None when unconfigured.

    `origin` is this product's own scheme+host (Settings.site_origin()), so a
    deployment serving both hostnames gets a redirect_uri that matches the
    host the user is actually on -- Google compares it literally, and a
    hotels user bounced to the flights hostname would be a mismatch error.
    Both must be registered in the Cloud Console; see README.
    """
    client_id = (
        client_id if client_id is not None else os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    ).strip()
    client_secret = (
        client_secret
        if client_secret is not None
        else os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    ).strip()
    if not client_id or not client_secret:
        return None
    return GoogleWebAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=f"{origin.rstrip('/')}/connect/callback",
        session_secret=derive_secret(master, b"fp-mcp-session-v1"),
        connect_secret=derive_secret(master, b"fp-mcp-connect-v1"),
    )
