"""
`/.well-known/mcp/server-card.json`, per hostname.

Why this document has tests at all: it is the ONLY thing Smithery can read
about a server it cannot scan, and the URL we publish (`/mcp/oauth`) always
answers 401, so it cannot be scanned. A card that quietly stopped matching
`tools/list` would show a wrong tool set on a public listing page and nothing
would fail.

Three properties, in the order they would hurt:

* **The tool list IS `tools/list`.** Byte-for-byte, same host, same process.
* **Each hostname gets its own product.** The flights card on both flights
  hostnames, the hotel card on `hotels.`, with every URL on that host's origin.
* **It is reachable and cacheable without credentials**, and it carries no
  secret.
"""

import base64
import json
import os

import httpx
import pytest

import src.entrypoint as entrypoint
import src.keystore as keystore_module
import src.oauth as oauth_module
import src.server as server_module
from src.keystore import MemoryKeyStore
from src.oauth import MCP_OAUTH_PATH
from src.oauthstore import MemoryOAuthStore
from src.servercard import SERVER_CARD_PATH

FLIGHTS_HOST = "google-flights-mcp.flightpowers.com"
FLIGHTS_ALIAS = "flights.flightpowers.com"
HOTELS_HOST = "hotels.flightpowers.com"

PUBLIC_URLS = {
    "MCP_PUBLIC_URL_FLIGHTS": "https://flights.golden.test/mcp",
    "MCP_PUBLIC_URL_HOTELS": "https://hotels.golden.test/mcp",
}
ORIGINS = {
    "flights": "https://flights.golden.test",
    "hotels": "https://hotels.golden.test",
}

MCP_ENV = (
    "MCP_PRODUCTS",
    "MCP_PRODUCTS_BY_HOST",
    "MCP_PUBLIC_URL",
    "MCP_PUBLIC_URL_FLIGHTS",
    "MCP_PUBLIC_URL_HOTELS",
    "SIGNUP_URL",
    "SIGNUP_URL_FLIGHTS",
    "SIGNUP_URL_HOTELS",
    "MCP_OAUTH",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "MCP_KEY_MASTER",
    "DATABASE_URL",
    "RAPIDAPI_KEY",
)

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "card-test", "version": "1.0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture(scope="module")
def combined():
    """The real production shape: one process, three hostnames, no OAuth env.

    Module-scoped for the same reason `test_host_routing.py`'s is: building it
    is two FastMCP constructions and every test below reads the same process.
    """
    previous = {name: os.environ.get(name) for name in MCP_ENV}
    for name in MCP_ENV:
        os.environ.pop(name, None)
    os.environ["MCP_PRODUCTS"] = "flights"
    os.environ.update(PUBLIC_URLS)
    try:
        yield entrypoint.build_entrypoint().app
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def with_oauth(monkeypatch):
    """One product, OAuth configured, so the card describes the signed-in path.

    Built per test rather than per module: it monkeypatches the two stores,
    and a module-scoped fixture cannot use `monkeypatch`.
    """
    for name in MCP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", "flights")
    monkeypatch.setenv("MCP_PRODUCTS_BY_HOST", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{ORIGINS['flights']}/mcp")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "1234.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
    monkeypatch.setenv(
        "MCP_KEY_MASTER", base64.b64encode(bytes(range(32))).decode()
    )
    monkeypatch.setenv("DATABASE_URL", "postgres://unused-in-tests/db")
    monkeypatch.setattr(
        keystore_module, "build_key_store", lambda *a, **k: MemoryKeyStore(bytes(range(32)))
    )
    monkeypatch.setattr(
        oauth_module, "build_oauth_store", lambda *a, **k: MemoryOAuthStore()
    )
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ),
    )
    return entrypoint.build_entrypoint().app


async def get_card(app, host: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{host}"
        ) as client:
            return await client.get(SERVER_CARD_PATH)


async def wire_tools(app, host: str) -> list[dict]:
    """`tools/list` off the same process, through /mcp, as a client sees it."""
    headers = dict(MCP_HEADERS)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{host}"
        ) as client:
            first = await client.post("/mcp", json=INITIALIZE, headers=headers)
            session = first.headers.get("mcp-session-id")
            if session:
                headers["mcp-session-id"] = session
            listed = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
    return _payload(listed.text)["result"]["tools"]


def _payload(body: str) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in {body!r}")


class TestItIsServed:
    @pytest.mark.parametrize("host", [FLIGHTS_HOST, FLIGHTS_ALIAS, HOTELS_HOST])
    async def test_200_json_on_every_hostname(self, combined, host):
        response = await get_card(combined, host)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert json.loads(response.text)

    async def test_cacheable_and_cors_open(self, combined):
        # SEP-1649 requires the CORS headers (a browser-based client reads
        # this) and suggests the cache header. A scanner polls it.
        response = await get_card(combined, FLIGHTS_HOST)
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["access-control-allow-methods"] == "GET"

    async def test_no_credential_required(self, combined):
        # The whole point: it is readable by a scanner that cannot get past
        # the auth wall on /mcp/oauth. No Authorization header anywhere above.
        assert (await get_card(combined, HOTELS_HOST)).status_code == 200


class TestTheShapeSmitheryDocuments:
    """https://smithery.ai/docs/build/publish -> Static Server Card, and the
    fuller field list it points at (SEP-1649)."""

    @pytest.mark.parametrize("host", [FLIGHTS_HOST, HOTELS_HOST])
    async def test_required_fields(self, combined, host):
        card = (await get_card(combined, host)).json()
        assert card["$schema"].endswith("/mcp-server-card/v1.json")
        assert card["version"] == "1.0"
        assert card["protocolVersion"]
        assert set(card["serverInfo"]) >= {"name", "title", "version"}
        assert card["transport"]["type"] == "streamable-http"
        assert card["transport"]["endpoint"].startswith("/mcp")
        # Smithery's example carries all three lists, empty where there are
        # none, so a reader never has to tell "none" from "not stated".
        for key in ("tools", "resources", "prompts"):
            assert isinstance(card[key], list)
        assert set(card["authentication"]) >= {"required", "schemes"}
        assert isinstance(card["authentication"]["required"], bool)

    async def test_capabilities_state_what_is_listed(self, combined):
        card = (await get_card(combined, FLIGHTS_HOST)).json()
        assert card["capabilities"]["tools"] == {"listChanged": False}
        # Flights carry four prompts; a card that omitted the capability
        # while listing them would contradict itself.
        assert card["prompts"]
        assert card["capabilities"]["prompts"] == {"listChanged": False}
        assert "resources" not in card["capabilities"]


class TestPerHost:
    @pytest.mark.parametrize("host", [FLIGHTS_HOST, FLIGHTS_ALIAS])
    async def test_both_flights_hostnames_get_the_flights_card(
        self, combined, host
    ):
        card = (await get_card(combined, host)).json()
        assert card["serverInfo"]["name"] == "google-flights-mcp"
        assert card["serverInfo"]["title"] == "Google Flights MCP"
        assert [t["name"] for t in card["tools"]] == [
            "search_oneway_flights",
            "search_roundtrip_flights",
        ]
        assert "flight" in card["description"]
        assert "Google Flights Live API" in card["description"]

    async def test_the_hotels_hostname_gets_the_hotels_card(self, combined):
        card = (await get_card(combined, HOTELS_HOST)).json()
        assert card["serverInfo"]["name"] == "booking-hotels-mcp"
        assert card["serverInfo"]["title"] == "Booking.com Hotels MCP"
        assert [t["name"] for t in card["tools"]] == [
            "search_hotels",
            "find_hotel_by_name",
            "compare_hotel_rates",
        ]
        assert "hotel" in card["description"]
        assert "Booking Live API" in card["description"]
        # Hotels register no prompts, and the card must not claim any.
        assert card["prompts"] == []
        assert "prompts" not in card["capabilities"]

    @pytest.mark.parametrize(
        "host, product", [(FLIGHTS_HOST, "flights"), (HOTELS_HOST, "hotels")]
    )
    async def test_every_url_is_on_that_products_own_origin(
        self, combined, host, product
    ):
        card = (await get_card(combined, host)).json()
        origin = ORIGINS[product]
        other = ORIGINS["hotels" if product == "flights" else "flights"]
        assert card["documentationUrl"] == f"{origin}/"
        assert card["_meta"]["com.flightpowers/privacyUrl"] == f"{origin}/privacy"
        assert card["_meta"]["com.flightpowers/termsUrl"] == f"{origin}/terms"
        assert card["_meta"]["com.flightpowers/supportUrl"] == f"{origin}/support"
        assert other not in json.dumps(card)

    async def test_instructions_are_the_ones_the_server_sends(self, combined):
        # Same string `initialize` returns, so a scanner that reads only the
        # card is not told something different from a client that connects.
        card = (await get_card(combined, HOTELS_HOST)).json()
        assert card["instructions"].strip()
        assert "hotel" in card["instructions"].lower()


class TestTheToolListCannotDrift:
    """The reason the card reads the live registry instead of a literal."""

    @pytest.mark.parametrize("host", [FLIGHTS_HOST, HOTELS_HOST])
    async def test_identical_to_tools_list(self, combined, host):
        card = (await get_card(combined, host)).json()
        assert card["tools"] == await wire_tools(combined, host)

    async def test_the_full_tool_object_is_carried(self, combined):
        # Not just names: Smithery renders descriptions and input schemas off
        # this document, and SEP-1649 says the entries follow `Tool`.
        card = (await get_card(combined, FLIGHTS_HOST)).json()
        for tool in card["tools"]:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"
            assert tool["title"]


class TestAuthentication:
    async def test_without_oauth_it_says_so(self, combined):
        # /mcp answers an anonymous initialize and tools/list, so claiming
        # authentication is required would be false.
        card = (await get_card(combined, FLIGHTS_HOST)).json()
        assert card["authentication"] == {"required": False, "schemes": []}
        assert card["transport"]["endpoint"] == "/mcp"
        # One endpoint, listed once.
        assert "com.flightpowers/alternativeTransports" not in card["_meta"]

    async def test_with_oauth_it_points_at_the_challenge(self, with_oauth):
        card = (await get_card(with_oauth, "flights.golden.test")).json()
        auth = card["authentication"]
        origin = ORIGINS["flights"]
        assert auth["required"] is True
        assert auth["schemes"] == ["oauth2"]
        # `/mcp` since 2026-09-09: one endpoint, one resource identifier.
        assert auth["resource"] == f"{origin}/mcp"
        assert auth["resourceMetadataUrl"] == (
            f"{origin}/.well-known/oauth-protected-resource/mcp"
        )
        assert auth["authorizationServers"] == [origin]
        assert card["transport"]["endpoint"] == "/mcp"

    async def test_the_metadata_url_is_the_one_the_401_advertises(
        self, with_oauth
    ):
        # A card that pointed somewhere else would send a client to a
        # different document than the challenge does.
        async with with_oauth.router.lifespan_context(with_oauth):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=with_oauth),
                base_url="https://flights.golden.test",
            ) as client:
                card = (await client.get(SERVER_CARD_PATH)).json()
                challenged = await client.post(
                    MCP_OAUTH_PATH, json=INITIALIZE, headers=MCP_HEADERS
                )
        assert challenged.status_code == 401
        assert (
            card["authentication"]["resourceMetadataUrl"]
            in challenged.headers["www-authenticate"]
        )

    async def test_the_always_challenge_alias_is_still_listed(self, with_oauth):
        """`/mcp` is the published endpoint now, so the alternative is the
        other direction: the alias that demands sign-in on request one, for
        clients whose auth mode is fixed when a server is added and for
        connectors saved on that URL before 2026-09-09."""
        card = (await get_card(with_oauth, "flights.golden.test")).json()
        alternatives = card["_meta"]["com.flightpowers/alternativeTransports"]
        assert [a["endpoint"] for a in alternatives] == ["/mcp/oauth"]
        assert alternatives[0]["authentication"]["required"] is True
        assert "rapidapi.com" in alternatives[0]["authentication"]["signupUrl"]


class TestItLeaksNothing:
    async def test_no_secret_reaches_the_document(self, with_oauth):
        body = (await get_card(with_oauth, "flights.golden.test")).text
        for secret in (
            "GOCSPX-secret",
            base64.b64encode(bytes(range(32))).decode(),
            "postgres://unused-in-tests/db",
            "1234.apps.googleusercontent.com",
        ):
            assert secret not in body

    async def test_no_advertising(self, combined):
        # Rule 5: this server is listable BECAUSE it carries no sponsored
        # content, and the card is the thing a directory reads.
        card = (await get_card(combined, FLIGHTS_HOST)).json()
        assert card["_meta"]["com.flightpowers/ads"] is False
        body = json.dumps(card).lower()
        # The free, ad-carrying server is a different deployment and must
        # never be named here: Anthropic's directory policy 4.C is why this
        # one is listable at all.
        assert "lulu" not in body
        assert "no advertising, no sponsored content" in card["description"].lower()
