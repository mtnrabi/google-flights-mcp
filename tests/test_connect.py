"""
/connect end to end: sign in, paste a key, search with the token, disconnect.

The fake identity provider is an httpx MockTransport standing in for Google's
token endpoint, and the same transport answers RapidAPI -- so this drives the
whole flow (redirect, state cookie, code exchange, validation call, storage,
connect token, a real tool call resolving the stored key, revocation) without
a network.

What is deliberately asserted here rather than trusted:

* an unconfigured deployment registers none of this and behaves exactly as it
  does today -- that is the property that makes the change safe to merge
  before the env vars exist;
* a request that carries its own key is never served from the store;
* a token whose row is gone produces "connect again", not "get a key" and
  not a search billed to somebody else.
"""

import base64
import json
import os
import re

import httpx
import pytest
from fastmcp import Client

import src.keystore as keystore_module
import src.server as server_module
from src.keystore import MemoryKeyStore
from src.server import build_server
from src.settings import Settings
from src.webauth import CONNECT_TOKEN_PREFIX, SESSION_COOKIE

CLIENT_ID = "1234.apps.googleusercontent.com"
MASTER_B64 = base64.b64encode(bytes(range(32))).decode()
USER_KEY = "user-key-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
HEADER_KEY = "header-key-abcdefghijklmnopqrstuvwxyz01234567"
ENV_KEY = "env-key-abcdefghijklmnopqrstuvwxyz0123456789ABC"

ONEWAY_ROW = {
    "price": "$209",
    "price_as_number": 209,
    "duration_seconds": 12600,
    "airline": "Wizz Air",
    "stops": 0,
    "buy_link": "https://google.test/a",
    "departure_date": "2026-09-20",
}


def make_settings(**overrides) -> Settings:
    base = dict(
        rapidapi_host="upstream.test",
        rapidapi_base_url="https://upstream.test",
        request_timeout_seconds=5.0,
        fallback_rapidapi_key="",
        max_searches_per_tool_call=5,
        max_concurrent_searches=3,
        max_http_connections=10,
        public_url="https://mcp.test/mcp",
        host="127.0.0.1",
        port=8000,
        log_path="",
        default_result_limit=10,
        signup_url="https://rapidapi.test/google-flights",
        products="flights",
    )
    base.update(overrides)
    return Settings(**base)


class Upstream:
    """Google's token endpoint, RapidAPI's gateway, and our own backend."""

    def __init__(self, sub="sub-1", email="a@example.test", validate_status=200):
        self.sub = sub
        self.email = email
        self.validate_status = validate_status
        self.keys_seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            def seg(obj):
                return (
                    base64.urlsafe_b64encode(json.dumps(obj).encode())
                    .decode()
                    .rstrip("=")
                )

            claims = {
                "sub": self.sub,
                "email": self.email,
                "email_verified": True,
                "aud": CLIENT_ID,
            }
            return httpx.Response(
                200,
                json={"id_token": f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"},
            )
        # Everything else is RapidAPI: the key-validation probe on the real
        # listing host, and the tool's own search on the test upstream.
        self.keys_seen.append(request.headers.get("x-rapidapi-key", ""))
        if "google-flights-live-api.p.rapidapi.com" in url:
            return httpx.Response(self.validate_status, json={"detail": "no dates"})
        return httpx.Response(200, json=[ONEWAY_ROW])


@pytest.fixture
def connected(monkeypatch):
    """A server with /connect configured and an in-memory key store."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
    monkeypatch.setenv("MCP_KEY_MASTER", MASTER_B64)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused-in-tests/db")

    store = MemoryKeyStore(bytes(range(32)))
    monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: store)

    upstream = Upstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(server_module, "_shared_client", client)

    mcp = build_server(make_settings())
    return mcp, store, upstream


@pytest.fixture
def unconfigured(monkeypatch):
    for name in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "MCP_KEY_MASTER",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
    )
    return build_server(make_settings())


def web(mcp) -> httpx.AsyncClient:
    """An HTTP client speaking to the server's own ASGI app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app(stateless_http=True)),
        base_url="https://mcp.test",
        follow_redirects=False,
    )


async def sign_in(http: httpx.AsyncClient) -> None:
    """Walk the real redirect flow and leave a session cookie on `http`."""
    start = await http.get("/connect/start")
    assert start.status_code == 302
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    # The cookie jar on the client carries fp_oauth from here.
    callback = await http.get(f"/connect/callback?code=code-1&state={state}")
    assert callback.status_code == 303
    assert SESSION_COOKIE in http.cookies


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def token_of(html: str) -> str:
    return re.search(r"fp_token=(fpk_[^\s&<]+)", html).group(1)


class TestUnconfiguredDeployment:
    """The state every deployment is in until ops sets four env vars."""

    @pytest.mark.asyncio
    async def test_connect_is_not_registered(self, unconfigured):
        async with web(unconfigured) as http:
            for path in ("/connect", "/connect/start", "/connect/callback"):
                assert (await http.get(path)).status_code == 404

    @pytest.mark.asyncio
    async def test_health_says_so(self, unconfigured):
        async with web(unconfigured) as http:
            body = (await http.get("/health")).json()
        assert body["connect_enabled"] is False

    @pytest.mark.asyncio
    async def test_a_keyless_call_is_unchanged(self, unconfigured):
        async with Client(unconfigured) as client:
            result = await client.call_tool(
                "search_oneway_flights",
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
            )
        assert result.structured_content["needs_api_key"] is True


class TestConfiguredDeployment:
    @pytest.mark.asyncio
    async def test_health_says_connect_is_on(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            body = (await http.get("/health")).json()
        assert body["connect_enabled"] is True

    @pytest.mark.asyncio
    async def test_signed_out_page_offers_google_and_both_listings(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            page = await http.get("/connect")
        assert page.status_code == 200
        assert "Sign in with Google" in page.text
        assert "/connect/start" in page.text
        assert "google-flights-live-api" in page.text
        assert "booking-live-api" in page.text
        assert "BASIC is free" in page.text

    @pytest.mark.asyncio
    async def test_start_redirects_to_google_with_pkce(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            response = await http.get("/connect/start")
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "code_challenge_method=S256" in location

    @pytest.mark.asyncio
    async def test_callback_without_state_does_not_sign_anyone_in(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            response = await http.get("/connect/callback?code=x&state=y")
        assert response.status_code == 400
        assert SESSION_COOKIE not in response.cookies

    @pytest.mark.asyncio
    async def test_cancelled_consent_is_a_sentence_not_an_error_page(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            response = await http.get("/connect/callback?error=access_denied")
        assert response.status_code == 200
        assert "cancelled" in response.text


class TestFullFlow:
    @pytest.mark.asyncio
    async def test_sign_in_paste_key_search_disconnect(self, connected):
        mcp, store, upstream = connected
        async with web(mcp) as http:
            await sign_in(http)

            page = (await http.get("/connect")).text
            assert "Paste your key" in page
            assert "at most one" in page  # the validation cost, stated up front

            saved = await http.post(
                "/connect/save",
                data={"csrf": csrf_of(page), "rapidapi_key": USER_KEY},
            )
            assert saved.status_code == 200
            # The key is stored, and the page shows only its last four.
            assert USER_KEY not in saved.text
            assert f"…{USER_KEY[-4:]}" in saved.text
            token = token_of(saved.text)

        stored = await store.get("sub-1")
        assert stored is not None and stored.key == USER_KEY
        assert stored.email == "a@example.test"
        # The validation probe went to the flights listing with the user's key.
        assert USER_KEY in upstream.keys_seen

        # A tool call carrying only the token resolves the stored key.
        upstream.keys_seen.clear()
        result = await call_with(mcp, {}, {"fp_token": token})
        assert result["result_count"] == 1
        assert upstream.keys_seen == [USER_KEY]

        # Disconnect deletes the row; the same token now asks them to reconnect.
        async with web(mcp) as http:
            await sign_in(http)
            page = (await http.get("/connect")).text
            gone = await http.post(
                "/connect/disconnect", data={"csrf": csrf_of(page)}
            )
        assert "Key removed" in gone.text
        assert await store.get("sub-1") is None

        upstream.keys_seen.clear()
        after = await call_with(mcp, {}, {"fp_token": token})
        assert after["needs_api_key"] is True
        assert "/connect" in after["message"]
        assert "disconnected" in after["message"]
        assert upstream.keys_seen == []

    @pytest.mark.asyncio
    async def test_a_rejected_key_is_not_stored(self, connected, monkeypatch):
        mcp, store, upstream = connected
        upstream.validate_status = 403
        async with web(mcp) as http:
            await sign_in(http)
            page = (await http.get("/connect")).text
            saved = await http.post(
                "/connect/save",
                data={"csrf": csrf_of(page), "rapidapi_key": USER_KEY},
            )
        assert "not subscribed" in saved.text
        assert await store.get("sub-1") is None

    @pytest.mark.asyncio
    async def test_an_unrecognised_key_is_not_stored(self, connected):
        mcp, store, upstream = connected
        upstream.validate_status = 401
        async with web(mcp) as http:
            await sign_in(http)
            page = (await http.get("/connect")).text
            saved = await http.post(
                "/connect/save",
                data={"csrf": csrf_of(page), "rapidapi_key": USER_KEY},
            )
        assert "did not recognise" in saved.text
        assert await store.get("sub-1") is None

    @pytest.mark.asyncio
    async def test_a_short_value_is_refused_before_any_request_is_spent(
        self, connected
    ):
        mcp, store, upstream = connected
        async with web(mcp) as http:
            await sign_in(http)
            page = (await http.get("/connect")).text
            saved = await http.post(
                "/connect/save", data={"csrf": csrf_of(page), "rapidapi_key": "abc"}
            )
        assert "does not look like" in saved.text
        assert upstream.keys_seen == []
        assert await store.get("sub-1") is None

    @pytest.mark.asyncio
    async def test_save_without_a_session_goes_back_to_the_page(self, connected):
        mcp, store, _ = connected
        async with web(mcp) as http:
            response = await http.post(
                "/connect/save", data={"csrf": "x", "rapidapi_key": USER_KEY}
            )
        assert response.status_code == 303
        assert await store.get("sub-1") is None

    @pytest.mark.asyncio
    async def test_a_wrong_csrf_token_saves_nothing(self, connected):
        mcp, store, _ = connected
        async with web(mcp) as http:
            await sign_in(http)
            saved = await http.post(
                "/connect/save",
                data={"csrf": "not-the-token", "rapidapi_key": USER_KEY},
            )
        assert "expired" in saved.text
        assert await store.get("sub-1") is None


async def call_with(mcp, headers: dict, params: dict) -> dict:
    """One flights tool call with a pretend HTTP request context."""
    import src.server as sm

    original = sm._request_context
    sm._request_context = lambda: (headers, params)
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "search_oneway_flights",
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
            )
        return result.structured_content
    finally:
        sm._request_context = original


class TestPrecedence:
    """The request always wins. This is the rule that keeps a leaked connect
    token from ever redirecting a working integration's spend."""

    @pytest.mark.asyncio
    async def test_a_header_key_beats_a_stored_one(self, connected):
        mcp, store, upstream = connected
        await store.put("sub-1", "a@example.test", USER_KEY)
        token = _token_for(mcp, "sub-1")

        upstream.keys_seen.clear()
        await call_with(mcp, {"x-rapidapi-key": HEADER_KEY}, {"fp_token": token})
        assert upstream.keys_seen == [HEADER_KEY]

    @pytest.mark.asyncio
    async def test_a_query_key_beats_a_stored_one(self, connected):
        mcp, store, upstream = connected
        await store.put("sub-1", "a@example.test", USER_KEY)
        token = _token_for(mcp, "sub-1")

        upstream.keys_seen.clear()
        await call_with(mcp, {}, {"rapidapi_key": HEADER_KEY, "fp_token": token})
        assert upstream.keys_seen == [HEADER_KEY]

    @pytest.mark.asyncio
    async def test_a_stored_key_beats_the_env_fallback(self, connected, monkeypatch):
        """The deployment's own key is the last resort, and must stay behind
        the caller's stored one -- otherwise a demo deployment with
        RAPIDAPI_KEY set would silently pay for every signed-in user."""
        mcp, store, upstream = connected
        settings = make_settings(fallback_rapidapi_key=ENV_KEY)
        monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: store)
        mcp = build_server(settings)
        await store.put("sub-2", "b@example.test", USER_KEY)
        token = _token_for(mcp, "sub-2")

        upstream.keys_seen.clear()
        await call_with(mcp, {}, {"fp_token": token})
        assert upstream.keys_seen == [USER_KEY]

    @pytest.mark.asyncio
    async def test_the_token_works_in_the_authorization_header_too(self, connected):
        mcp, store, upstream = connected
        await store.put("sub-1", "a@example.test", USER_KEY)
        token = _token_for(mcp, "sub-1")

        upstream.keys_seen.clear()
        await call_with(mcp, {"authorization": f"Bearer {token}"}, {})
        assert upstream.keys_seen == [USER_KEY]

    @pytest.mark.asyncio
    async def test_a_token_pasted_where_the_key_goes_still_works(self, connected):
        """Users paste the token into the box the key used to go in. It must
        not be forwarded to RapidAPI as a key."""
        mcp, store, upstream = connected
        await store.put("sub-1", "a@example.test", USER_KEY)
        token = _token_for(mcp, "sub-1")

        upstream.keys_seen.clear()
        await call_with(mcp, {"x-rapidapi-key": token}, {})
        assert upstream.keys_seen == [USER_KEY]

    @pytest.mark.asyncio
    async def test_an_unknown_token_falls_through_to_the_keyless_reply(
        self, connected
    ):
        mcp, _, upstream = connected
        upstream.keys_seen.clear()
        result = await call_with(mcp, {}, {"fp_token": CONNECT_TOKEN_PREFIX + "junk"})
        assert result["needs_api_key"] is True
        assert upstream.keys_seen == []


def _token_for(mcp, sub: str) -> str:
    """A connect token for `sub`, minted the way /connect mints one."""
    from src.webauth import build_web_auth

    auth = build_web_auth(
        mcp.settings_obj.site_origin(), bytes(range(32)), CLIENT_ID, "GOCSPX-secret"
    )
    assert auth is not None
    return auth.issue_connect_token(sub)


class TestHotelsToo:
    @pytest.mark.asyncio
    async def test_a_stored_key_serves_the_hotel_tools(self, connected, monkeypatch):
        mcp, store, upstream = connected
        monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: store)
        both = build_server(make_settings(products="both"))
        await store.put("sub-1", "a@example.test", USER_KEY)
        token = _token_for(both, "sub-1")

        upstream.keys_seen.clear()
        import src.server as sm

        original = sm._request_context
        sm._request_context = lambda: ({}, {"fp_token": token})
        try:
            async with Client(both) as client:
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Athens",
                        "checkin_date": "2026-09-20",
                        "checkout_date": "2026-09-22",
                    },
                )
        finally:
            sm._request_context = original
        assert upstream.keys_seen == [USER_KEY]


class TestAliasHosts:
    """`flights.flightpowers.com` is the same deployment as
    `google-flights-mcp.flightpowers.com` -- but a different cookie origin,
    and Google compares redirect_uri literally."""

    @pytest.mark.asyncio
    async def test_an_alias_is_sent_to_the_canonical_origin_first(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            page = await http.get("/connect", headers={"host": "alias.test"})
            start = await http.get("/connect/start", headers={"host": "alias.test"})
        assert page.status_code == 302
        assert page.headers["location"] == "https://mcp.test/connect"
        assert start.status_code == 302
        assert start.headers["location"] == "https://mcp.test/connect"

    @pytest.mark.asyncio
    async def test_the_canonical_host_is_served_directly(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            page = await http.get("/connect", headers={"host": "mcp.test"})
        assert page.status_code == 200
        assert "Sign in with Google" in page.text


class TestDiscovery:
    """A feature nobody can find is a feature nobody uses."""

    @pytest.mark.asyncio
    async def test_a_keyless_reply_points_at_connect_when_it_exists(self, connected):
        mcp, _, _ = connected
        result = await call_with(mcp, {}, {})
        assert result["how_to_get_a_key"]["connect_url"] == "https://mcp.test/connect"
        # The three original steps are untouched: a model that already knows
        # how to relay this reply keeps working.
        assert len(result["how_to_get_a_key"]["how"]) == 3

    @pytest.mark.asyncio
    async def test_no_connect_url_where_there_is_no_connect_page(self, unconfigured):
        result = await call_with(unconfigured, {}, {})
        assert "connect_url" not in result["how_to_get_a_key"]
