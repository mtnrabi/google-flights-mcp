"""Which RapidAPI account owns a stored key (migrations/005, 2026-11-23).

The flights backend echoes the Hub's `X-RapidAPI-User` as `X-FP-User` on our
keyed calls; the server records it next to the signed-in account. The things
that would cost something if they broke:

* the username is recorded only for a key WE hold for a signed-in account,
  never for a header key or for the keyless allowance (our own key);
* it never reaches the reply;
* a store that cannot write it never fails the search.
"""

import httpx
import pytest

from src.keystore import MemoryKeyStore
from src.rapidapi_client import USER_ECHO_HEADER, RapidAPIClient
from tests.test_oauth import EMAIL, HEADER_KEY, MASTER, SUB, USER_KEY, Session, _granted_access_token, call_tool
from tests.test_trial import SEARCH_ARGS, TaggedUpstream, _build_trial

HOST = "google-flights-live-api.p.rapidapi.com"
ROW = {"price": "$56", "price_as_number": 56, "airline": "Test Air"}


class EchoingUpstream(TaggedUpstream):
    """The harness upstream, answering every search with an `X-FP-User`."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        if "oauth2.googleapis.com" in str(request.url):
            return response
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={**response.headers, USER_ECHO_HEADER: "leaver-AbC123"},
        )


@pytest.fixture
def echo(monkeypatch):
    deployment = _build_trial(monkeypatch)
    upstream = EchoingUpstream()
    import src.server as server_module

    monkeypatch.setattr(
        server_module, "_shared_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    )
    deployment.upstream = upstream
    return deployment


class TestTheClientReadsTheEcho:
    async def test_user_sink_gets_the_header(self):
        def handler(request):
            return httpx.Response(200, json=[ROW], headers={USER_ECHO_HEADER: "someone-1"})

        client = RapidAPIClient(f"https://{HOST}", HOST, 5.0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        sink: list[str] = []
        async with client:
            await client.search("oneway", {"x": 1}, api_key="k" * 40, user_sink=sink)
        assert sink == ["someone-1"]

    async def test_no_header_no_entry(self):
        def handler(request):
            return httpx.Response(200, json=[ROW])

        client = RapidAPIClient(f"https://{HOST}", HOST, 5.0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        sink: list[str] = []
        async with client:
            await client.search("oneway", {"x": 1}, api_key="k" * 40, user_sink=sink)
        assert sink == []


class TestTheMemoryStore:
    async def test_note_sets_the_user_on_a_live_row_only(self):
        store = MemoryKeyStore(MASTER)
        await store.note_rapidapi_user("nobody", "x")  # no row: a no-op
        await store.put(SUB, EMAIL, USER_KEY)
        await store.note_rapidapi_user(SUB, "leaver-AbC123")
        assert store._rows[("google", SUB)]["rapidapi_user"] == "leaver-AbC123"


class TestTheServerRecordsIt:
    async def test_a_stored_key_records_the_owner(self, echo):
        await echo.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(echo) as session:
            token = await _granted_access_token(session.http)
            reply = await call_tool(session.http, "/mcp", SEARCH_ARGS, {"authorization": f"Bearer {token}"})
        assert echo.upstream.keys_seen == [USER_KEY]
        assert echo.key_store._rows[("google", SUB)]["rapidapi_user"] == "leaver-AbC123"
        assert "leaver-AbC123" not in str(reply)

    async def test_the_allowance_records_nothing(self, echo):
        async with Session(echo) as session:
            token = await _granted_access_token(session.http)
            await call_tool(session.http, "/mcp", SEARCH_ARGS, {"authorization": f"Bearer {token}"})
        assert echo.key_store._rows == {}

    async def test_a_header_key_records_nothing(self, echo):
        await echo.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(echo) as session:
            token = await _granted_access_token(session.http)
            await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}", "x-rapidapi-key": HEADER_KEY},
            )
        assert "rapidapi_user" not in echo.key_store._rows[("google", SUB)]

    async def test_a_store_that_fails_never_fails_the_search(self, echo, monkeypatch):
        await echo.key_store.put(SUB, EMAIL, USER_KEY)

        async def boom(*a, **k):
            raise RuntimeError("column rapidapi_user does not exist")

        monkeypatch.setattr(echo.key_store, "note_rapidapi_user", boom)
        async with Session(echo) as session:
            token = await _granted_access_token(session.http)
            reply = await call_tool(session.http, "/mcp", SEARCH_ARGS, {"authorization": f"Bearer {token}"})
        assert reply.get("result_count") == 1
