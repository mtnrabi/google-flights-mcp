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
import html as html_module
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
    MCP_PATH,
    REFRESH_TOKEN_PREFIX,
    pkce_challenge,
)
from src.oauthstore import AuthCode, MemoryOAuthStore, TokenRecord, hash_secret
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


def bounced_params(response) -> dict[str, str]:
    """What the client is told, whichever way the server chose to tell it.

    Every client registered in this file uses a loopback `redirect_uri`
    (REDIRECT_URI is `http://127.0.0.1:33418/callback`, which is what a real
    native client uses), so an error is rendered as our own page with the
    RFC redirect offered as a link rather than followed automatically --
    see `oauth.is_loopback_redirect`. The parameters are the same either
    way, and they are what these tests are about.
    """
    if response.status_code in (302, 303):
        return query_of(response.headers["location"])
    assert response.status_code == 200, response.status_code
    href = re.search(
        r'<a href="([^"]+)">Tell it you cancelled</a>', response.text
    ).group(1)
    return query_of(html_module.unescape(href))


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
            # `/mcp`, whichever path was challenged: one server, one audience.
            assert resource["resource"] == f"{ORIGIN}{MCP_PATH}"
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

            # The first access token is untouched by a rotation ALONE: it
            # expires on its own clock, which is what lets a client refresh
            # early without dropping an in-flight call.
            live.upstream.keys_seen.clear()
            still = await call_tool(
                http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {"authorization": f"Bearer {access}"},
            )
            assert still["result_count"] == 1

            # A rotated refresh token coming back ONCE, seconds later, is
            # read as the retry it almost always is: the same pair the
            # rotation already issued, created nothing new (day 3's grace
            # window). It also spends the one benign replay.
            retried = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                },
            )
            assert retried.status_code == 200
            assert retried.json()["refresh_token"] == second["refresh_token"]

            # Coming back a SECOND time is the one thing that DOES take the
            # family. A rotated token replayed after the retry window means
            # either a client in a loop or a copy in somebody else's hands,
            # and day 3 assumes the second: same invalid_grant, plus the
            # whole family.
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

            gone = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={**MCP_HEADERS, "authorization": f"Bearer {access}"},
            )
            assert gone.status_code == 401

            # 10. Revocation of an already-dead token still answers 200
            #     (RFC 7009 2.2), and the endpoint stays a 401.
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


class TestPlainMcpServesEveryCredential:
    """`/mcp` is the single endpoint since 2026-09-09.

    The promise that used to be here -- "`/mcp` never 401s" -- was replaced
    deliberately: it made the sign-in invisible to every client's auth
    machinery, so a user with no key saw "the tool failed" and had nowhere to
    click. The promise that replaced it is narrower and is the one that
    protects paying integrations: **a request carrying any credential is
    never challenged.**
    """

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

    async def test_a_wrong_key_is_still_a_reply_and_not_a_challenge(self, live):
        """The question the gate asks is "did the caller bring something",
        not "does it work". A broken key has to reach the tool layer, which
        is the only place that can say RapidAPI refused it -- replacing that
        precise error with a sign-in prompt would be a worse answer."""
        async with Session(live) as session:
            result = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                # Long enough to be a key, wrong enough to be refused. A
                # value under MIN_KEY_LENGTH is treated as absent, which is a
                # different case (and the one the challenge is for).
                {"x-rapidapi-key": "wrong-key-abcdefghijklmnopqrstuvwxyz0123"},
            )
        # It reached the tools. Whether RapidAPI then accepts the key is the
        # tool layer's business and the stub upstream's; the point here is
        # that the gate did not turn a key problem into a sign-in prompt.
        assert isinstance(result, dict)
        assert "error" not in result

    async def test_a_query_param_key_is_a_credential(self, live):
        async with Session(live) as session:
            response = await session.http.post(
                f"/mcp?rapidapi_key={HEADER_KEY}",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=dict(MCP_HEADERS),
            )
        assert response.status_code == 200

    async def test_a_caller_with_nothing_is_challenged(self, live):
        """The change. A keyless caller used to get a 200 whose body said
        `needs_api_key`, which no client's auth machinery can see."""
        async with Session(live) as session:
            response = await session.http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=dict(MCP_HEADERS),
            )
        assert response.status_code == 401
        header = response.headers["www-authenticate"]
        # Pointed at `/mcp`'s own document, which is the URL it asked about.
        assert header.endswith(
            f'oauth-protected-resource{MCP_PATH}"'
        ), header
        # And the body says both ways in, for the reader who never sees the
        # header: a script, a log, a person running curl.
        body = response.json()
        assert "sign in" in body["error_description"].lower()
        assert "x-rapidapi-key" in body["error_description"]

    async def test_the_challenge_can_be_switched_off_without_a_deploy(
        self, live, monkeypatch
    ):
        """`MCP_REQUIRE_AUTH=off` is the rollback: `/mcp` stops challenging
        and answers a keyless caller with the old `needs_api_key` body."""
        monkeypatch.setenv("MCP_REQUIRE_AUTH", "off")
        async with Session(live) as session:
            result = await call_tool(session.http, "/mcp", SEARCH_ARGS)
        assert result["needs_api_key"] is True
        assert "No RapidAPI key was supplied" in result["message"]

    async def test_the_alias_challenges_whatever_the_mode_says(
        self, live, monkeypatch
    ):
        """A rollback that silently turned the always-challenge URL into an
        open one would strand every client that added it expecting a Sign in
        button."""
        monkeypatch.setenv("MCP_REQUIRE_AUTH", "off")
        async with Session(live) as session:
            response = await session.http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=dict(MCP_HEADERS),
            )
        assert response.status_code == 401

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
        """Two things at once, and both matter.

        The header is stripped BEFORE the challenge, so forging it does not
        even buy an anonymous caller a 200 -- they get the 401. And when the
        same caller brings a real key, the forged subject still resolves
        nothing: the stored key of `SUB` is never spent, only the key the
        caller actually sent.
        """
        await live.key_store.put(SUB, EMAIL, USER_KEY)
        forged = {"x-fp-oauth-subject": SUB, "x-fp-oauth-provider": "google"}
        async with Session(live) as session:
            challenged = await session.http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**dict(MCP_HEADERS), **forged},
            )
            assert challenged.status_code == 401
            assert live.upstream.keys_seen == []

            result = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {**forged, "x-rapidapi-key": HEADER_KEY},
            )
        assert result["result_count"] == 1
        assert live.upstream.keys_seen == [HEADER_KEY]

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


class TestATokenIsBoundToTheResourceItWasApprovedFor:
    """The two products share one deployment, one database and one stored
    RapidAPI key per user, so a token approved on the flights consent page --
    which says "search live flight fares" and nothing else -- must not be
    accepted on the hotels hostname and spend the user's hotels plan. MCP
    2025-06-18 requires a resource server to check that a token was issued
    for it; the `resource` column exists for that and this is where it is
    read."""

    @staticmethod
    async def _token_for(store, resource: str) -> str:
        token = oauth_module.mint(ACCESS_TOKEN_PREFIX)
        await store.put_token(
            TokenRecord(
                token_hash=hash_secret(token),
                kind="access",
                client_id="fpcl_probe",
                user_sub=SUB,
                provider="google",
                scope="flightpowers:search",
                resource=resource,
                expires_at=time.time() + 3600,
            )
        )
        return token

    async def test_a_token_for_the_other_product_is_refused(self, live):
        await live.key_store.put(SUB, EMAIL, USER_KEY)
        token = await self._token_for(
            live.oauth_store, "https://hotels.flightpowers.test/mcp/oauth"
        )
        async with Session(live) as session:
            response = await session.http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={**MCP_HEADERS, "authorization": f"Bearer {token}"},
            )
        assert response.status_code == 401
        assert "invalid_token" in response.headers["www-authenticate"]
        # The point of the test: no search ran and no key was spent.
        assert live.upstream.keys_seen == []

    @pytest.mark.parametrize(
        "resource",
        [ORIGIN, f"{ORIGIN}/", f"{ORIGIN}/mcp", f"{ORIGIN}{MCP_OAUTH_PATH}", ""],
    )
    async def test_every_spelling_of_this_server_still_works(self, live, resource):
        """The check is strict about the host and forgiving about the path:
        clients in the wild send all of these for the same server."""
        await live.key_store.put(SUB, EMAIL, USER_KEY)
        token = await self._token_for(live.oauth_store, resource)
        async with Session(live) as session:
            result = await call_tool(
                session.http,
                MCP_OAUTH_PATH,
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert result["result_count"] == 1
        assert live.upstream.keys_seen == [USER_KEY]


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
        params = bounced_params(response)
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
        assert bounced_params(response)["error"] == "invalid_request"


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
        assert bounced_params(response)["error"] == "invalid_target"

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
        params = bounced_params(denied)
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
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource/mcp/oauth",
        ],
    )
    async def test_protected_resource(self, live, path):
        async with Session(live) as session:
            body = (await session.http.get(path)).json()
        # ONE resource identifier whichever document you read. A token
        # audience that changed with the path would fail on the other one --
        # "sign-in worked and then nothing works".
        assert body["resource"] == f"{ORIGIN}{MCP_PATH}"
        assert body["authorization_servers"] == [ORIGIN]

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
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
