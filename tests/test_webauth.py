"""
Signed tokens, and the Google exchange, against a fake identity provider.

Two properties matter more than the happy path:

* a connect token must never be mistaken for a RapidAPI key, in any of the
  six places a key can arrive (that half is asserted in test_credentials.py);
* a session cookie and a connect token must not be interchangeable, even
  though they are the same construction, because a session cookie is minutes
  long and a connect token is months long.
"""

import base64
import json
import time

import httpx
import pytest

from src.webauth import (
    CONNECT_TOKEN_PREFIX,
    GoogleIdentity,
    GoogleWebAuth,
    WebAuthError,
    build_web_auth,
    derive_secret,
    sign_payload,
    verify_payload,
)

MASTER = bytes(range(32))
CLIENT_ID = "1234.apps.googleusercontent.com"


def make_auth(origin: str = "https://mcp.test") -> GoogleWebAuth:
    auth = build_web_auth(origin, MASTER, CLIENT_ID, "GOCSPX-secret")
    assert auth is not None
    return auth


def id_token(sub: str, email: str, aud: str = CLIENT_ID, verified: bool = True) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return ".".join(
        [
            seg({"alg": "RS256"}),
            seg(
                {
                    "sub": sub,
                    "email": email,
                    "email_verified": verified,
                    "aud": aud,
                    "iss": "https://accounts.google.com",
                }
            ),
            "signature-not-checked-here",
        ]
    )


class TestSigning:
    def test_round_trip(self):
        secret = derive_secret(MASTER, b"label")
        token = sign_payload({"a": 1, "exp": time.time() + 60}, secret)
        assert verify_payload(token, secret)["a"] == 1

    def test_tampered_payload_is_rejected(self):
        secret = derive_secret(MASTER, b"label")
        token = sign_payload({"sub": "1", "exp": time.time() + 60}, secret)
        body, _, mac = token.partition(".")
        forged = (
            base64.urlsafe_b64encode(
                json.dumps({"sub": "2", "exp": time.time() + 60}).encode()
            )
            .decode()
            .rstrip("=")
        )
        with pytest.raises(WebAuthError):
            verify_payload(f"{forged}.{mac}", secret)

    def test_another_secret_cannot_sign(self):
        token = sign_payload({"exp": time.time() + 60}, derive_secret(MASTER, b"a"))
        with pytest.raises(WebAuthError):
            verify_payload(token, derive_secret(MASTER, b"b"))

    def test_expired_is_rejected(self):
        secret = derive_secret(MASTER, b"label")
        token = sign_payload({"exp": time.time() - 1}, secret)
        with pytest.raises(WebAuthError):
            verify_payload(token, secret)

    def test_no_expiry_is_rejected(self):
        secret = derive_secret(MASTER, b"label")
        token = sign_payload({"a": 1}, secret)
        with pytest.raises(WebAuthError):
            verify_payload(token, secret)

    def test_garbage_is_rejected(self):
        secret = derive_secret(MASTER, b"label")
        for bad in ("", "no-dot", "a.b", "...."):
            with pytest.raises(WebAuthError):
                verify_payload(bad, secret)

    def test_session_and_connect_secrets_differ(self):
        auth = make_auth()
        assert auth.session_secret != auth.connect_secret


class TestConnectTokens:
    def test_round_trip(self):
        auth = make_auth()
        token = auth.issue_connect_token("sub-1")
        assert token.startswith(CONNECT_TOKEN_PREFIX)
        assert auth.read_connect_token(token) == "sub-1"

    def test_a_session_cookie_is_not_a_connect_token(self):
        """Different secret AND a different `typ`. Either one alone would
        do; both, because a session cookie lives for an hour and a connect
        token for ninety days, and confusing them the wrong way round is a
        three-month credential handed out by a page."""
        auth = make_auth()
        session = auth.issue_session(GoogleIdentity("sub-1", "a@example.test"))
        assert auth.read_connect_token(session) is None
        assert auth.read_connect_token(CONNECT_TOKEN_PREFIX + session) is None

    def test_a_connect_token_is_not_a_session(self):
        auth = make_auth()
        token = auth.issue_connect_token("sub-1")
        assert auth.read_session(token) is None

    def test_expired_token_resolves_to_nothing(self):
        auth = make_auth()
        token = auth.issue_connect_token("sub-1", now=time.time() - 10**9)
        assert auth.read_connect_token(token) is None

    def test_unprefixed_value_is_never_read(self):
        """A RapidAPI key handed to this function must come back None, or a
        real key would be looked up as if it were a token."""
        auth = make_auth()
        assert auth.read_connect_token("a" * 50) is None

    def test_a_token_from_another_master_key_is_rejected(self):
        """Rotating MCP_KEY_MASTER invalidates outstanding connect tokens at
        the same moment it stops the stored ciphertext decrypting -- which is
        the behaviour you want, because after a rotation everybody
        reconnects."""
        token = make_auth().issue_connect_token("sub-1")
        other = build_web_auth("https://mcp.test", bytes(32), CLIENT_ID, "s")
        assert other is not None
        assert other.read_connect_token(token) is None


class TestSessions:
    def test_round_trip(self):
        auth = make_auth()
        cookie = auth.issue_session(GoogleIdentity("sub-1", "a@example.test"))
        identity = auth.read_session(cookie)
        assert identity is not None
        assert identity.sub == "sub-1"
        assert identity.email == "a@example.test"

    def test_missing_cookie_is_none(self):
        assert make_auth().read_session(None) is None
        assert make_auth().read_session("") is None

    def test_expired_session_is_none(self):
        auth = make_auth()
        cookie = auth.issue_session(GoogleIdentity("sub-1", "a@example.test"))
        assert auth.read_session(cookie, now=time.time() + 10**6) is None


class TestAuthorizeUrl:
    def test_asks_for_two_scopes_and_nothing_else(self):
        url, _ = make_auth().start()
        assert "scope=openid+email" in url or "scope=openid%20email" in url
        for unwanted in ("drive", "calendar", "profile", "contacts"):
            assert unwanted not in url

    def test_uses_pkce(self):
        url, _ = make_auth().start()
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url

    def test_redirect_uri_is_this_origin(self):
        url, _ = make_auth("https://hotels.flightpowers.com").start()
        assert "hotels.flightpowers.com%2Fconnect%2Fcallback" in url

    def test_does_not_ask_for_offline_access(self):
        """We never call Google again on the user's behalf, so a refresh
        token would be a credential held for no reason."""
        url, _ = make_auth().start()
        assert "access_type=online" in url


class FakeGoogle:
    """The token endpoint, and nothing else."""

    def __init__(self, sub="sub-1", email="a@example.test", aud=CLIENT_ID, status=200):
        self.sub, self.email, self.aud, self.status = sub, email, aud, status
        self.seen: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        self.seen = dict(
            pair.split("=", 1)
            for pair in request.content.decode().split("&")
            if "=" in pair
        )
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "invalid_grant"})
        return httpx.Response(
            200, json={"id_token": id_token(self.sub, self.email, self.aud)}
        )


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestExchange:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        auth = make_auth()
        url, cookie = auth.start()
        state = url.split("state=")[1].split("&")[0]
        google = FakeGoogle()
        async with client_for(google) as client:
            identity = await auth.finish("code-1", state, cookie, client=client)
        assert identity == GoogleIdentity("sub-1", "a@example.test")
        assert google.seen["code_verifier"]
        assert google.seen["grant_type"] == "authorization_code"

    @pytest.mark.asyncio
    async def test_state_mismatch_is_rejected(self):
        """The CSRF property of the whole flow. A callback whose state does
        not match the cookie is somebody else's sign-in being replayed into
        this browser."""
        auth = make_auth()
        _, cookie = auth.start()
        async with client_for(FakeGoogle()) as client:
            with pytest.raises(WebAuthError):
                await auth.finish("code-1", "not-the-state", cookie, client=client)

    @pytest.mark.asyncio
    async def test_forged_state_cookie_is_rejected(self):
        auth = make_auth()
        forged = sign_payload(
            {"typ": "oauth", "state": "s", "v": "v", "exp": time.time() + 60},
            derive_secret(b"\x00" * 32, b"fp-mcp-session-v1"),
        )
        async with client_for(FakeGoogle()) as client:
            with pytest.raises(WebAuthError):
                await auth.finish("code-1", "s", forged, client=client)

    @pytest.mark.asyncio
    async def test_google_refusing_the_code_is_an_error_not_an_identity(self):
        auth = make_auth()
        url, cookie = auth.start()
        state = url.split("state=")[1].split("&")[0]
        async with client_for(FakeGoogle(status=400)) as client:
            with pytest.raises(WebAuthError):
                await auth.finish("code-1", state, cookie, client=client)

    @pytest.mark.asyncio
    async def test_id_token_for_another_app_is_rejected(self):
        """Audience pinning. A token minted for a different client_id is not
        a statement about a user of ours."""
        auth = make_auth()
        url, cookie = auth.start()
        state = url.split("state=")[1].split("&")[0]
        async with client_for(FakeGoogle(aud="someone-else")) as client:
            with pytest.raises(WebAuthError):
                await auth.finish("code-1", state, cookie, client=client)

    @pytest.mark.asyncio
    async def test_unverified_email_keeps_the_account_and_drops_the_address(self):
        auth = make_auth()
        url, cookie = auth.start()
        state = url.split("state=")[1].split("&")[0]

        def handler(request):
            return httpx.Response(
                200,
                json={"id_token": id_token("sub-1", "a@example.test", verified=False)},
            )

        async with client_for(handler) as client:
            identity = await auth.finish("code-1", state, cookie, client=client)
        assert identity.sub == "sub-1"
        assert identity.email == ""


class TestBuildWebAuth:
    def test_unconfigured_is_none(self):
        assert build_web_auth("https://mcp.test", MASTER, "", "") is None
        assert build_web_auth("https://mcp.test", MASTER, "id", "") is None
        assert build_web_auth("https://mcp.test", MASTER, "", "secret") is None
