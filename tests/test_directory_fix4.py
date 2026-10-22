"""
Directory fix round 2 on PR #534 (2026-10-22).

1. A free signed-in search (the keyless allowance) never shows OUR key's
   RapidAPI quota. The no-key route test answered "This search used 1 of your
   RapidAPI plan's requests; 44475 of 50000 remain" and the card footer read
   "44,475 left on your plan": FlightPowers' own plan, shown to a user who has
   none, and to any directory reviewer. A keyed search still shows the
   caller's own plan.
2. /connect after Save says only what the page that follows actually shows,
   and every server page names app@flightpowers.com.
3. Model-facing text is true for a free user too: no invoice, no "billed to
   the caller's own plan" as the only case, and the month-wide advice is
   scoped to a key; `sort_by` says what "best" does.
"""

import json

import httpx
import pytest

import src.server as server_module
from fastmcp import Client

from src.legal import CONTACT_EMAIL, render_document, support_html
from src.server import _sort_phrase, build_instructions
from src.trial import TrialState, allowance_usage
from tests.test_connect_ux import (  # noqa: F401 - `live` is a fixture
    Session as UxSession,
    authorize_query,
    csrf_of,
    live,
    register,
    sign_in,
)
from tests.test_connect_ux import USER_KEY as UX_KEY
from tests.test_oauth import HEADER_KEY, Session, _payload, call_tool, mcp_request
from tests.test_server import build_with_upstream, make_settings
from tests.test_trial import (
    HOTEL_ARGS,
    SEARCH_ARGS,
    TRIAL_KEY,
    TaggedUpstream,
    _bearer,
    _build_trial,
)

#: The figures the 2026-10-22 route test saw on our own key.
OUR_REMAINING = 44475
OUR_LIMIT = 50000


class QuotaUpstream(TaggedUpstream):
    """The fake RapidAPI, answering with the quota headers the real Hub sends."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        if str(request.url).startswith("https://oauth2.googleapis.com/token"):
            return response
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={
                "content-type": "application/json",
                "x-ratelimit-requests-limit": str(OUR_LIMIT),
                "x-ratelimit-requests-remaining": str(OUR_REMAINING),
                "x-ratelimit-requests-reset": "86400",
            },
        )


def _with_quota(monkeypatch, **kwargs):
    deployment = _build_trial(monkeypatch, cap=10, **kwargs)
    upstream = QuotaUpstream()
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    deployment.upstream = upstream
    return deployment


@pytest.fixture
def flights(monkeypatch):
    return _with_quota(monkeypatch)


@pytest.fixture
def hotels(monkeypatch):
    return _with_quota(monkeypatch, products="hotels")


def _no_plan_figures(reply: dict) -> None:
    usage = reply["api_usage"]
    dumped = json.dumps(usage)
    for word in ("plan_requests", "hub_requests_billed", "plan_seconds"):
        assert word not in dumped, word
    assert str(OUR_REMAINING) not in json.dumps(reply)
    assert "44,475" not in json.dumps(reply)
    assert "RapidAPI plan" not in usage["note"]
    assert "billed" not in usage["note"]
    assert "renews at 00:00 UTC" in usage["note"]


# ── 1. the allowance never shows our quota ───────────────────────────────


class TestAFreeSearchNeverShowsOurQuota:
    async def test_flights(self, flights):
        async with Session(flights) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http, "/mcp", SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert flights.upstream.keys_seen == [TRIAL_KEY]
        _no_plan_figures(reply)
        usage = reply["api_usage"]
        assert usage["free_searches_used_today"] == 1
        assert usage["free_searches_day_cap"] == 10
        assert usage["free_searches_remaining_today"] == 9
        assert usage["requests_used_by_this_call"] == 1
        assert "1 of 10 used today" in usage["note"]
        assert "hub_requests_billed" not in reply["search_coverage"]
        # The trial block is unchanged.
        assert reply["trial"]["used_today"] == 1

    async def test_hotels_single_search(self, hotels):
        async with Session(hotels) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http, "/mcp", HOTEL_ARGS,
                {"authorization": f"Bearer {token}"}, tool="search_hotels",
            )
        assert reply["trial"]["key_connected"] is False
        _no_plan_figures(reply)
        assert reply["api_usage"]["free_searches_used_today"] == 1

    async def test_hotels_checkin_range(self, hotels):
        args = {
            "destination": "Rome",
            "checkin_date_from": "2026-10-10",
            "checkin_date_to": "2026-10-11",
            "nights": 2,
        }
        async with Session(hotels) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http, "/mcp", args,
                {"authorization": f"Bearer {token}"}, tool="search_hotels",
            )
        assert "trial" in reply
        _no_plan_figures(reply)
        assert reply["api_usage"]["requests_used_by_this_call"] == 2
        assert reply["api_usage"]["free_searches_used_today"] == 2

    async def test_a_keyed_search_still_shows_the_callers_own_plan(self, flights):
        async with Session(flights) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http, "/mcp", SEARCH_ARGS,
                {"authorization": f"Bearer {token}", "x-rapidapi-key": HEADER_KEY},
            )
        assert flights.upstream.keys_seen == [HEADER_KEY]
        assert "trial" not in reply
        usage = reply["api_usage"]
        assert usage["plan_requests_remaining"] == OUR_REMAINING
        assert usage["plan_requests_limit"] == OUR_LIMIT
        assert usage["hub_requests_billed"] == 1
        assert "of your RapidAPI plan's requests" in usage["note"]
        assert "free_searches_used_today" not in usage


class TestAllowanceUsage:
    def test_the_block(self):
        state = TrialState(user_sub="s", email="a@b.test", used_today=4, day_cap=10)
        usage = allowance_usage(state, 3)
        assert usage == {
            "requests_used_by_this_call": 3,
            "free_searches_used_today": 4,
            "free_searches_day_cap": 10,
            "free_searches_remaining_today": 6,
            "note": (
                "This search used 3 of the free searches; 4 of 10 used today "
                "on a@b.test, 6 left. The allowance renews at 00:00 UTC."
            ),
        }


class TestTheCardFooter:
    def test_the_widget_reads_the_trial_block_first(self):
        from src.widget import FLIGHTS_WIDGET_HTML

        footer = FLIGHTS_WIDGET_HTML[FLIGHTS_WIDGET_HTML.index("function footerOf"):]
        footer = footer[: footer.index("/* ── render ── */")]
        trial_at = footer.index("sc.trial")
        assert trial_at < footer.index("plan_requests_remaining")
        assert "free searches used today · renews at 00:00 UTC" in footer

    def test_rendered(self):
        from tests.test_widget_render import (
            _chrome_or_skip,
            _inline,
            _rows,
            _run_page,
        )

        probe = """
<script>
(function () {
  window.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                       params: { structuredContent: __PAYLOAD__ } }, "*");
  setTimeout(function () {
    var f = document.querySelector(".fp-ft");
    var out = { footer: f ? f.textContent.trim() : null };
    document.title = "RESULT:" + btoa(unescape(encodeURIComponent(JSON.stringify(out))));
  }, 120);
})();
</script>
"""
        rows = [r for r in _rows() if r["to_airport"] == "Budapest (BUD)"][:3]
        base = {
            "search_status": "ok",
            "result_count": len(rows),
            "search_coverage": {
                "destinations_searched": ["Budapest (BUD)"],
                "departure_dates_searched": ["2026-11-10"],
            },
            "results": rows,
        }
        state = TrialState(user_sub="s", email="", used_today=3, day_cap=10)
        free = dict(base, api_usage=allowance_usage(state, 1), trial={
            "used_today": 3, "day_cap": 10, "remaining_today": 7,
            "key_connected": False,
        })
        # A free payload that somehow still carried plan figures must not
        # show them either: the trial block wins.
        free["api_usage"]["plan_requests_remaining"] = OUR_REMAINING
        keyed = dict(base, api_usage={
            "requests_used_by_this_call": 1,
            "plan_requests_remaining": 46152,
        })
        chrome = _chrome_or_skip()

        def footer(payload):
            return _run_page(
                chrome, probe.replace("__PAYLOAD__", _inline(payload)), width=760
            )["footer"]

        assert footer(free) == "3 of 10 free searches used today · renews at 00:00 UTC"
        assert "46,152 left on your plan" in footer(keyed)


# ── 2. /connect says what is true, and the contact is app@ ───────────────


class TestTheSaveNotice:
    async def test_signed_in_from_a_client_no_url_promised(self, live):  # noqa: F811
        async with UxSession(live) as session:
            http = session.http
            client_id = await register(http)
            await sign_in(
                http, next_path=f"/connect/authorize?{authorize_query(client_id)}"
            )
            page = await http.get("/connect")
            saved = await http.post(
                "/connect/save",
                data={"csrf": csrf_of(page.text), "rapidapi_key": UX_KEY},
            )
        assert "Key saved." in saved.text
        assert "Copy the connect URL" not in saved.text
        assert "fp_token=" not in saved.text

    async def test_the_token_reader_still_gets_the_url(self, live):  # noqa: F811
        async with UxSession(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
            saved = await session.http.post(
                "/connect/save",
                data={"csrf": csrf_of(page.text), "rapidapi_key": UX_KEY},
            )
        assert "Copy the connect URL below" in saved.text
        assert "fp_token=" in saved.text


class TestTheContact:
    def test_every_page_names_app(self):
        assert CONTACT_EMAIL == "app@flightpowers.com"
        for html in (
            render_document("privacy"),
            render_document("terms"),
            support_html(),
        ):
            assert "app@flightpowers.com" in html
            assert "matan@" not in html

    async def test_the_connect_page_names_app(self, live):  # noqa: F811
        async with UxSession(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert "Questions: app@flightpowers.com" in page.text
        assert "matan@" not in page.text


# ── 3. model-facing text is true for a free user ─────────────────────────


async def _trial_tools(deployment) -> dict:
    """tools/list on a deployment with OAuth and the allowance on."""
    async with Session(deployment) as session:
        token = await _bearer(session.http)
        auth = {"authorization": f"Bearer {token}"}
        started = await mcp_request(session.http, "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fix4", "version": "1.0"},
            },
        }, auth)
        sid = started.headers.get("mcp-session-id")
        extra = dict(auth, **({"mcp-session-id": sid} if sid else {}))
        listed = await mcp_request(session.http, "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }, extra)
    return {t["name"]: t for t in _payload(listed.text)["result"]["tools"]}


async def _plain_tools() -> dict:
    mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return {t.name: t.model_dump(mode="json") for t in tools}


class TestFreeUserText:
    def test_instructions_with_the_allowance(self):
        text = build_instructions(
            make_settings(), trial_tail="10 free searches a day."
        )
        assert "invoice" not in text
        assert "billed to the caller's own plan" not in text
        assert "nothing is billed to the user" in text
        assert "narrow a wide" in text
        assert "free-search counter" in text

    def test_instructions_without_the_allowance_say_nothing_of_it(self):
        text = build_instructions(make_settings())
        assert "free sign-in allowance" not in text
        assert "free-search counter" not in text
        assert "caller's own RapidAPI plan and what is left of it" in text

    async def test_the_month_advice_is_scoped_to_a_key_on_the_allowance(
        self, flights
    ):
        desc = (await _trial_tools(flights))["search_roundtrip_flights"][
            "description"
        ]
        assert "That holds with the caller's own key." in desc
        assert "narrow it to fit" in desc
        assert "whole month needs their own key" in desc

    async def test_no_allowance_line_without_the_allowance(self):
        desc = (await _plain_tools())["search_roundtrip_flights"]["description"]
        assert "free sign-in" not in desc
        assert "do not narrow the question to make it fit" in desc

    def test_api_usage_schema_names_both_cases(self):
        from src.output_schema import _API_USAGE

        assert "invoice" not in json.dumps(_API_USAGE)
        assert "free signed-in search" in _API_USAGE["description"]
        for name in (
            "free_searches_used_today",
            "free_searches_day_cap",
            "free_searches_remaining_today",
        ):
            assert name in _API_USAGE["properties"]


class TestSortBy:
    def test_best_is_said_as_cheapest_first(self):
        assert _sort_phrase("best") == "price (cheapest first)"
        assert _sort_phrase("price") == "price (cheapest first)"
        assert _sort_phrase("duration") == "duration (shortest first)"

    async def test_schema_says_what_best_does(self):
        tools = await _plain_tools()
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            prop = tools[name]["inputSchema"]["properties"]["sort_by"]
            assert prop["default"] == "best"
            assert "cheapest first" in prop["description"]
            assert "best first" not in prop["description"]
