"""
MCP-protocol OAuth, driven the way a client drives it: raw HTTP against the
Vercel entrypoint.

Not `fastmcp.Client` and not a product's own `http_app`. The whole feature
lives in two places a unit test cannot see -- the ASGI gate in
`src/entrypoint.py` that strips the injected identity header, challenges
`/mcp/oauth` and rewrites the path, and the browser flow that ends in a
redirect back to a client's `redirect_uri` -- so every test here goes through
`api.index.app` with its lifespan run, exactly as production does.

The full flow, once, end to end (`test_the_whole_flow`): register, get
challenged, sign in, approve, exchange the code, call a tool, refresh, revoke.
The rest of the file is the ways it must fail.

Three properties are asserted here because they are the ones that would cost
money or trust if they broke:

* **`/mcp` never changes.** No 401, no challenge, keys and `fpk_` tokens
  unaffected, an anonymous `tools/list` still answered. A regression there is
  an outage for every paying caller.
* **The injected identity header cannot be forged.** `_resolve` serves a
  user's stored RapidAPI key on the strength of `x-fp-oauth-subject`, so a
  caller able to set it himself could spend a stranger's plan.
* **PKCE is enforced and a code is single-use.** Both are what stand between
  an intercepted authorization code and somebody else's subscription.
"""

import base64
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

import api.index as entrypoint_module
import src.entrypoint as entrypoint
import src.keystore as keystore_module
import src.oauth as oauth_module
import src.server as server_module
from src.keystore import MemoryKeyStore
from src.oauth import (
    ACCESS_TOKEN_PREFIX,
    CODE_TTL_SECONDS,
    MCP_OAUTH_PATH,
    REFRESH_TOKEN_PREFIX,
    pkce_challenge,
)
from src.oauthstore import AuthCode, MemoryOAuthStore, hash_secret
from src.webauth import SESSION_COOKIE

GOOGLE_CLIENT_ID = "1234.apps.googleusercontent.com"
MASTER = bytes(range(32))
MASTER_B64 = base64.b64encode(MASTER).decode()
USER_KEY = "user-key-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
HEADER_KEY = "header-key-abcdefghijklmnopqrstuvwxyz01234567"
SUB = "sub-1"
EMAIL = "a@example.test"
ORIGIN = "https://mcp.test"
REDIRECT_URI = "http://127.0.0.1:33418/callback"
VERIFIER = "verifier-" + "a" * 50

ONEWAY_ROW = {
    "price": "$209",
    "price_as_number": 209,
    "duration_seconds": 12600,
    "airline": "Wizz Air",
    "stops": 0,
    "buy_link": "https://google.test/a",
    "departure_date": "2026-09-20",
}

SEARCH_ARGS = {
    "departure_date": "2026-09-20",
    "from_airport": "TLV",
    "to_airport": "ATH",
}

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


class Upstream:
    """Google's token endpoint and RapidAPI, without a network."""

    def __init__(self) -> None:
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
                "sub": SUB,
                "email": EMAIL,
                "email_verified": True,
                "aud": GOOGLE_CLIENT_ID,
            }
            return httpx.Response(
                200, json={"id_token": f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"}
            )
        self.keys_seen.append(request.headers.get("x-rapidapi-key", ""))
        if "google-flights-live-api.p.rapidapi.com" in url:
            return httpx.Response(200, json={"detail": "no dates"})
        return httpx.Response(200, json=[ONEWAY_ROW])


class Deployment:
    """One built process plus the stores behind it."""

    def __init__(self, app, key_store, oauth_store, upstream) -> None:
        self.app = app
        self.key_store = key_store
        self.oauth_store = oauth_store
        self.upstream = upstream


def _build(monkeypatch, *, configured: bool = True, oauth_flag: str | None = None):
    for name in (
        "MCP_PRODUCTS",
        "MCP_PRODUCTS_BY_HOST",
        "MCP_PUBLIC_URL",
        "MCP_PUBLIC_URL_FLIGHTS",
        "MCP_PUBLIC_URL_HOTELS",
        "SIGNUP_URL",
        "RAPIDAPI_KEY",
        "MCP_OAUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", "flights")
    # One product, one hostname: the host map is exercised by
    # tests/test_host_routing.py and would only add noise here.
    monkeypatch.setenv("MCP_PRODUCTS_BY_HOST", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{ORIGIN}/mcp")
    monkeypatch.setenv("RAPIDAPI_BASE_URL", "https://upstream.test")
    monkeypatch.setenv("RAPIDAPI_HOST", "upstream.test")
    if oauth_flag is not None:
        monkeypatch.setenv("MCP_OAUTH", oauth_flag)

    key_store = MemoryKeyStore(MASTER)
    oauth_store = MemoryOAuthStore()
    if configured:
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
        monkeypatch.setenv("MCP_KEY_MASTER", MASTER_B64)
        monkeypatch.setenv("DATABASE_URL", "postgres://unused-in-tests/db")
        monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: key_store)
        monkeypatch.setattr(
            oauth_module, "build_oauth_store", lambda *a, **k: oauth_store
        )
    else:
        for name in (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "MCP_KEY_MASTER",
            "DATABASE_URL",
        ):
            monkeypatch.delenv(name, raising=False)

    upstream = Upstream()
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    return Deployment(
        entrypoint.build_entrypoint().app, key_store, oauth_store, upstream
    )


@pytest.fixture
def live(monkeypatch):
    return _build(monkeypatch)


@pytest.fixture
def unconfigured(monkeypatch):
    return _build(monkeypatch, configured=False)


@pytest.fixture
def oauth_off(monkeypatch):
    return _build(monkeypatch, oauth_flag="off")


class Session:
    """An httpx client bound to one deployment, with its lifespan run."""

    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment
        self._lifespan = None
        self.http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Session":
        app = self.deployment.app
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.http is not None
        await self.http.aclose()
        assert self._lifespan is not None
        await self._lifespan.__aexit__(*exc)


def _payload(body: str) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in {body!r}")


async def mcp_request(http, path, message, headers=None, expect=200):
    response = await http.post(
        path, json=message, headers={**MCP_HEADERS, **(headers or {})}
    )
    assert response.status_code == expect, response.text
    return response


async def call_tool(http, path, args, headers=None, tool="search_oneway_flights"):
    """initialize, then one tools/call, over the real transport."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "oauth-test", "version": "1.0"},
        },
    }
    started = await mcp_request(http, path, initialize, headers)
    session = started.headers.get("mcp-session-id")
    extra = dict(headers or {})
    if session:
        extra["mcp-session-id"] = session
    response = await mcp_request(
        http,
        path,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        extra,
    )
    result = _payload(response.text)["result"]
    return json.loads(result["content"][0]["text"])


async def register_client(http, **overrides) -> dict:
    body = {
        "client_name": "Cursor",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    body.update(overrides)
    response = await http.post("/oauth/register", json=body)
    return response.status_code, response.json()


def authorize_query(client_id: str, **overrides) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": pkce_challenge(VERIFIER),
        "code_challenge_method": "S256",
        "scope": "flightpowers:search",
        "state": "state-123",
        "resource": f"{ORIGIN}{MCP_OAUTH_PATH}",
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in list(params.items()):
        if value is None:
            del params[key]
    return urlencode(params)


async def sign_in(http, next_path: str = "") -> httpx.Response:
    """Walk the day-1 Google redirect flow; leaves a session cookie."""
    query = f"?{urlencode({'next': next_path})}" if next_path else ""
    start = await http.get(f"/connect/start{query}")
    assert start.status_code == 302
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    callback = await http.get(f"/connect/callback?code=code-1&state={state}")
    assert callback.status_code == 303
    assert SESSION_COOKIE in http.cookies
    return callback


def field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"no {name} field in the page"
    return match.group(1)


def query_of(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


# ── the flow ─────────────────────────────────────────────────────────────


class TestTheWholeFlow:
    async def test_register_authorize_token_call_refresh_revoke(self, live):
        async with Session(live) as session:
            http = session.http

            # 1. The 401 that makes a client start a sign-in at all.
            challenged = await http.get(MCP_OAUTH_PATH)
            assert challenged.status_code == 401
            challenge = challenged.headers["www-authenticate"]
            assert challenge.startswith("Bearer ")
            assert (
                f'resource_metadata="{ORIGIN}/.well-known/'
                f'oauth-protected-resource{MCP_OAUTH_PATH}"' in challenge
            )

            # 2. Discovery, following the URL the challenge gave.
            metadata_url = re.search(
                r'resource_metadata="([^"]+)"', challenge
            ).group(1)
            resource = (await http.get(urlsplit(metadata_url).path)).json()
            assert resource["resource"] == f"{ORIGIN}{MCP_OAUTH_PATH}"
            assert resource["authorization_servers"] == [ORIGIN]

            server = (
                await http.get("/.well-known/oauth-authorization-server")
            ).json()
            assert server["issuer"] == ORIGIN
            assert server["code_challenge_methods_supported"] == ["S256"]

            # 3. Dynamic client registration.
            status, registered = await register_client(http)
            assert status == 201
            client_id = registered["client_id"]
            assert "client_secret" not in registered  # public client

            # 4. /authorize with no session hands off to the Google sign-in
            #    and comes back to the same request.
            query = authorize_query(client_id)
            bounced = await http.get(f"{server['authorization_endpoint']}?{query}")
            assert bounced.status_code == 302
            assert bounced.headers["location"].startswith("/connect/start?next=")
            next_path = query_of(bounced.headers["location"])["next"]
            assert next_path.startswith("/connect/authorize?")

            landed = await sign_in(http, next_path)
            assert landed.headers["location"].startswith("/connect/authorize?")

            # 5. The consent page, and approval.
            page = await http.get(landed.headers["location"])
            assert page.status_code == 200
            assert "Cursor" in page.text
            assert "your own RapidAPI plan" in page.text
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page.text, "csrf"),
                    "request": field(page.text, "request"),
                    "decision": "approve",
                },
            )
            assert approved.status_code == 303
            back = approved.headers["location"]
            assert back.startswith(REDIRECT_URI)
            returned = query_of(back)
            assert returned["state"] == "state-123"
            code = returned["code"]

            # 6. The code exchange.
            tokens = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": VERIFIER,
                },
            )
            assert tokens.status_code == 200
            assert tokens.headers["cache-control"] == "no-store"
            issued = tokens.json()
            access = issued["access_token"]
            refresh = issued["refresh_token"]
            assert access.startswith(ACCESS_TOKEN_PREFIX)
            assert refresh.startswith(REFRESH_TOKEN_PREFIX)
            assert issued["token_type"] == "Bearer"
            assert issued["expires_in"] == 3600

            # 7. Signed in but no key yet: the reply says so, with the URL,
            #    and nothing is searched.
            live.upstream.keys_seen.clear()
            keyless = await call_tool(
                http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {"authorization": f"Bearer {access}"},
            )
            assert keyless["needs_api_key"] is True
            assert f"{ORIGIN}/connect" in keyless["message"]
            assert "no RapidAPI key is connected" in keyless["message"]
            assert live.upstream.keys_seen == []

            # 8. With a key connected, the same call searches, billed to it.
            await live.key_store.put(SUB, EMAIL, USER_KEY)
            result = await call_tool(
                http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {"authorization": f"Bearer {access}"},
            )
            assert result["result_count"] == 1
            assert live.upstream.keys_seen == [USER_KEY]

            # 9. Refresh rotates: a new pair, and the old refresh token dies.
            refreshed = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                },
            )
            assert refreshed.status_code == 200
            second = refreshed.json()
            assert second["access_token"] != access
            assert second["refresh_token"] != refresh

            replayed = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                },
            )
            assert replayed.status_code == 400
            assert replayed.json()["error"] == "invalid_grant"

            # The first access token is untouched by a rotation: it expires
            # on its own clock, which is what lets a client refresh early
            # without dropping an in-flight call.
            live.upstream.keys_seen.clear()
            still = await call_tool(
                http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {"authorization": f"Bearer {access}"},
            )
            assert still["result_count"] == 1

            # 10. Revocation, and the 401 that follows it.
            revoked = await http.post(
                "/oauth/revoke",
                data={"token": second["access_token"], "client_id": client_id},
            )
            assert revoked.status_code == 200
            after = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {second['access_token']}",
                },
            )
            assert after.status_code == 401
            assert 'error="invalid_token"' in after.headers["www-authenticate"]


# ── /mcp is untouched ────────────────────────────────────────────────────


class TestPlainMcpIsUnchanged:
    async def test_a_keyed_call_never_sees_a_challenge(self, live):
        async with Session(live) as session:
            result = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"x-rapidapi-key": HEADER_KEY},
            )
        assert result["result_count"] == 1
        assert live.upstream.keys_seen == [HEADER_KEY]

    async def test_a_keyless_call_is_still_a_reply_not_a_401(self, live):
        async with Session(live) as session:
            result = await call_tool(session.http, "/mcp", SEARCH_ARGS)
        assert result["needs_api_key"] is True
        # The get-a-key reply, not the reconnect one: this caller has no
        # identity at all, so telling them to sign in again would be wrong.
        assert "No RapidAPI key was supplied" in result["message"]

    async def test_tools_list_is_still_anonymous(self, live):
        async with Session(live) as session:
            started = await mcp_request(
                session.http,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
            )
            headers = dict(MCP_HEADERS)
            if started.headers.get("mcp-session-id"):
                headers["mcp-session-id"] = started.headers["mcp-session-id"]
            listed = await session.http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=headers,
            )
        assert listed.status_code == 200
        assert _payload(listed.text)["result"]["tools"]

    async def test_a_header_key_still_beats_an_oauth_identity(self, live):
        """The request always wins -- the rule that protects paying
        integrations from a token someone else's session left behind."""
        async with Session(live) as session:
            http = session.http
            await live.key_store.put(SUB, EMAIL, USER_KEY)
            access = await _granted_access_token(http)
            live.upstream.keys_seen.clear()
            result = await call_tool(
                http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {
                    "authorization": f"Bearer {access}",
                    "x-rapidapi-key": HEADER_KEY,
                },
            )
        assert result["result_count"] == 1
        assert live.upstream.keys_seen == [HEADER_KEY]


class TestTheInjectedHeaderCannotBeForged:
    async def test_a_forged_subject_on_plain_mcp_resolves_nothing(self, live):
        await live.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(live) as session:
            result = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"x-fp-oauth-subject": SUB, "x-fp-oauth-provider": "google"},
            )
        assert result["needs_api_key"] is True
        assert live.upstream.keys_seen == []

    async def test_a_forged_subject_on_the_oauth_endpoint_is_still_401(self, live):
        await live.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(live) as session:
            response = await session.http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={**MCP_HEADERS, "x-fp-oauth-subject": SUB},
            )
        assert response.status_code == 401
        assert live.upstream.keys_seen == []


# ── the ways it must fail ────────────────────────────────────────────────


async def _granted_access_token(http, **authorize_overrides) -> str:
    """register → sign in → approve → exchange, returning the access token."""
    _, registered = await register_client(http)
    client_id = registered["client_id"]
    query = authorize_query(client_id, **authorize_overrides)
    await sign_in(http)
    page = await http.get(f"/connect/authorize?{query}")
    assert page.status_code == 200, page.text[:400]
    approved = await http.post(
        "/connect/authorize",
        data={
            "csrf": field(page.text, "csrf"),
            "request": field(page.text, "request"),
            "decision": "approve",
        },
    )
    code = query_of(approved.headers["location"])["code"]
    tokens = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return tokens.json()["access_token"]


class TestPkce:
    async def test_a_wrong_verifier_is_refused(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            client_id = registered["client_id"]
            await sign_in(http)
            page = await http.get(f"/connect/authorize?{authorize_query(client_id)}")
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page.text, "csrf"),
                    "request": field(page.text, "request"),
                    "decision": "approve",
                },
            )
            code = query_of(approved.headers["location"])["code"]

            refused = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": "not-the-verifier-" + "b" * 40,
                },
            )
            assert refused.status_code == 400
            assert refused.json()["error"] == "invalid_grant"

            # And the code is burned by the attempt, so the real client
            # cannot use it either. Consuming before checking is deliberate:
            # a code that has been presented has been presented.
            retried = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": VERIFIER,
                },
            )
        assert retried.status_code == 400
        assert retried.json()["error"] == "invalid_grant"

    async def test_authorize_without_pkce_is_bounced_to_the_client(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            query = authorize_query(
                registered["client_id"], code_challenge="", code_challenge_method=""
            )
            response = await http.get(f"/connect/authorize?{query}")
        assert response.status_code == 302
        params = query_of(response.headers["location"])
        assert response.headers["location"].startswith(REDIRECT_URI)
        assert params["error"] == "invalid_request"
        assert params["state"] == "state-123"

    async def test_plain_is_not_accepted(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            query = authorize_query(
                registered["client_id"],
                code_challenge=VERIFIER,
                code_challenge_method="plain",
            )
            response = await http.get(f"/connect/authorize?{query}")
        assert response.status_code == 302
        assert query_of(response.headers["location"])["error"] == "invalid_request"


class TestCodeLifetime:
    async def test_an_expired_code_is_refused(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            client_id = registered["client_id"]
            code = "fpc_expired-code-value"
            await live.oauth_store.put_code(
                AuthCode(
                    code_hash=hash_secret(code),
                    client_id=client_id,
                    redirect_uri=REDIRECT_URI,
                    code_challenge=pkce_challenge(VERIFIER),
                    scope="flightpowers:search",
                    user_sub=SUB,
                    provider="google",
                    resource=f"{ORIGIN}{MCP_OAUTH_PATH}",
                    expires_at=time.time() - CODE_TTL_SECONDS - 1,
                )
            )
            response = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": VERIFIER,
                },
            )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert "expired" in response.json()["error_description"]

    async def test_a_code_cannot_be_replayed(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            client_id = registered["client_id"]
            await sign_in(http)
            page = await http.get(f"/connect/authorize?{authorize_query(client_id)}")
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page.text, "csrf"),
                    "request": field(page.text, "request"),
                    "decision": "approve",
                },
            )
            code = query_of(approved.headers["location"])["code"]
            body = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": VERIFIER,
            }
            first = await http.post("/oauth/token", data=body)
            second = await http.post("/oauth/token", data=body)
        assert first.status_code == 200
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"


class TestAuthorizeValidation:
    async def test_an_unknown_client_renders_a_page_and_never_redirects(self, live):
        async with Session(live) as session:
            response = await session.http.get(
                f"/connect/authorize?{authorize_query('fpcl_nope')}"
            )
        assert response.status_code == 400
        assert "location" not in response.headers
        assert "not registered" in response.text

    async def test_an_unregistered_redirect_uri_renders_a_page(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            query = authorize_query(
                registered["client_id"], redirect_uri="https://evil.test/steal"
            )
            response = await http.get(f"/connect/authorize?{query}")
        assert response.status_code == 400
        assert "location" not in response.headers

    async def test_a_foreign_resource_is_refused(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            query = authorize_query(
                registered["client_id"], resource="https://someone-else.test/mcp"
            )
            response = await http.get(f"/connect/authorize?{query}")
        assert response.status_code == 302
        assert query_of(response.headers["location"])["error"] == "invalid_target"

    async def test_deny_sends_access_denied_back(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            await sign_in(http)
            page = await http.get(
                f"/connect/authorize?{authorize_query(registered['client_id'])}"
            )
            denied = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page.text, "csrf"),
                    "request": field(page.text, "request"),
                    "decision": "deny",
                },
            )
        assert denied.status_code == 303
        params = query_of(denied.headers["location"])
        assert params["error"] == "access_denied"
        assert params["state"] == "state-123"

    async def test_a_tampered_consent_form_approves_nothing(self, live):
        async with Session(live) as session:
            http = session.http
            _, registered = await register_client(http)
            await sign_in(http)
            page = await http.get(
                f"/connect/authorize?{authorize_query(registered['client_id'])}"
            )
            sealed = field(page.text, "request")
            response = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page.text, "csrf"),
                    # One character flipped in the signed blob.
                    "request": sealed[:-2] + ("A" if sealed[-2] != "A" else "B") + sealed[-1],
                    "decision": "approve",
                },
            )
        assert response.status_code == 400
        assert "location" not in response.headers

    async def test_the_next_hop_cannot_be_an_open_redirect(self, live):
        """`next` rides inside the signed state cookie and is path-only."""
        async with Session(live) as session:
            http = session.http
            landed = await sign_in(http, "https://evil.test/steal")
        assert landed.headers["location"] == "/connect"


class TestRegistration:
    async def test_a_confidential_client_gets_a_secret_once(self, live):
        async with Session(live) as session:
            http = session.http
            status, registered = await register_client(
                http, token_endpoint_auth_method="client_secret_post"
            )
            assert status == 201
            secret = registered["client_secret"]
            assert registered["client_secret_expires_at"] == 0

            # And it is required: the same flow without it is refused.
            refused = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "fpc_whatever",
                    "client_id": registered["client_id"],
                    "code_verifier": VERIFIER,
                },
            )
            assert refused.status_code == 401
            assert refused.json()["error"] == "invalid_client"

            accepted = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "fpc_whatever",
                    "client_id": registered["client_id"],
                    "client_secret": secret,
                    "code_verifier": VERIFIER,
                },
            )
            # Past client auth, and refused on the code instead.
            assert accepted.status_code == 400
            assert accepted.json()["error"] == "invalid_grant"

    async def test_a_cleartext_remote_redirect_uri_is_refused(self, live):
        async with Session(live) as session:
            status, body = await register_client(
                session.http, redirect_uris=["http://evil.test/callback"]
            )
        assert status == 400
        assert body["error"] == "invalid_redirect_uri"

    async def test_loopback_http_is_allowed(self, live):
        async with Session(live) as session:
            status, _ = await register_client(
                session.http, redirect_uris=["http://localhost:9999/cb"]
            )
        assert status == 201

    async def test_a_private_use_scheme_is_allowed(self, live):
        async with Session(live) as session:
            status, _ = await register_client(
                session.http, redirect_uris=["cursor://anysphere.cursor-mcp/oauth"]
            )
        assert status == 201

    async def test_no_redirect_uris_is_refused(self, live):
        async with Session(live) as session:
            status, body = await register_client(session.http, redirect_uris=[])
        assert status == 400
        assert body["error"] == "invalid_redirect_uri"


class TestDisconnect:
    async def test_disconnect_drops_every_oauth_token(self, live):
        async with Session(live) as session:
            http = session.http
            await live.key_store.put(SUB, EMAIL, USER_KEY)
            access = await _granted_access_token(http)

            page = (await http.get("/connect")).text
            gone = await http.post(
                "/connect/disconnect",
                data={"csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1)},
            )
            assert "Key removed" in gone.text

            response = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={**MCP_HEADERS, "authorization": f"Bearer {access}"},
            )
        assert response.status_code == 401


# ── off by absence ───────────────────────────────────────────────────────


class TestUnconfiguredDeployment:
    """The state every deployment is in until ops sets the env vars."""

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/connect/authorize",
        ],
    )
    async def test_no_oauth_routes(self, unconfigured, path):
        async with Session(unconfigured) as session:
            assert (await session.http.get(path)).status_code == 404

    async def test_the_oauth_endpoint_says_so_rather_than_challenging(
        self, unconfigured
    ):
        async with Session(unconfigured) as session:
            response = await session.http.get(MCP_OAUTH_PATH)
        assert response.status_code == 404
        assert "www-authenticate" not in response.headers
        assert "/mcp with a RapidAPI key" in response.json()["error_description"]

    async def test_health_says_so(self, unconfigured):
        async with Session(unconfigured) as session:
            body = (await session.http.get("/health")).json()
        assert body["oauth_enabled"] is False
        assert body["oauth_mcp_endpoint"] is None

    async def test_plain_mcp_still_works(self, unconfigured):
        async with Session(unconfigured) as session:
            result = await call_tool(
                session.http, "/mcp", SEARCH_ARGS, {"x-rapidapi-key": HEADER_KEY}
            )
        assert result["result_count"] == 1


class TestTheKillSwitch:
    async def test_mcp_oauth_off_leaves_connect_alone(self, oauth_off):
        async with Session(oauth_off) as session:
            http = session.http
            assert (await http.get(MCP_OAUTH_PATH)).status_code == 404
            assert (await http.get("/connect")).status_code == 200
            body = (await http.get("/health")).json()
        assert body["oauth_enabled"] is False
        assert body["connect_enabled"] is True


class TestHealthAdvertisesTheEndpoint:
    async def test_it_names_the_url_to_publish(self, live):
        async with Session(live) as session:
            body = (await session.http.get("/health")).json()
        assert body["oauth_enabled"] is True
        assert body["oauth_mcp_endpoint"] == f"{ORIGIN}{MCP_OAUTH_PATH}"


class TestMetadataIsServedBothWays:
    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp/oauth",
        ],
    )
    async def test_protected_resource(self, live, path):
        async with Session(live) as session:
            body = (await session.http.get(path)).json()
        assert body["resource"] == f"{ORIGIN}{MCP_OAUTH_PATH}"
        assert body["authorization_servers"] == [ORIGIN]

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp/oauth",
        ],
    )
    async def test_authorization_server(self, live, path):
        async with Session(live) as session:
            body = (await session.http.get(path)).json()
        assert body["registration_endpoint"] == f"{ORIGIN}/oauth/register"
        assert body["authorization_endpoint"] == f"{ORIGIN}/connect/authorize"
        assert set(body["grant_types_supported"]) == {
            "authorization_code",
            "refresh_token",
        }


class TestEntrypointStillExports(object):
    """A guard on the wiring: the gate must not have replaced the app."""

    def test_the_exported_app_is_still_the_starlette_wrapper(self):
        assert hasattr(entrypoint_module.app, "router")
