"""The flights result card: what it must be, and what it must not touch.

Four properties, in the order they would hurt:

* **It renders at all.** claude.ai validates `_meta.ui.domain` against a
  hash of the connector URL the user added and refuses the frame on a
  mismatch, with no error anywhere. That failure was live on the free
  server from launch until 2026-09-15. This deployment serves several
  hostnames from one process, so the domain is asserted PER HOST, on
  `resources/list` AND on `resources/read` -- claude.ai validates on the
  read, and a middleware that only handled the list would leave the red
  chip exactly where it was.
* **Nothing changes for a host that does not render UI.** The tool
  RESULT must be byte-for-byte what it was: Cursor, Claude Code, curl,
  Smithery and every script are the paying traffic.
* **No ads.** Both paid listings are directory-listed, and an
  ad-carrying server can never be. The served bytes must reach no
  third-party origin at all.
* **Flights only.** The hotels tools and the hotels hostname are
  untouched.
"""

import hashlib
import json

import httpx
import pytest

import src.entrypoint as entrypoint
import src.server as server_module
from fastmcp import Client
from src.settings import Settings
from src.widget import (
    FLIGHTS_WIDGET_HTML,
    ORIGINAL_PATH_SCOPE_KEY,
    WIDGET_MIME_TYPE,
    WIDGET_URI,
    canonical_connector_url,
    claude_apps_domain,
)
from src.widget_domain import HostWidgetDomainMiddleware, connector_url_from_request

KEY = "k" * 32
FLIGHTS_HOST = "google-flights-mcp.flightpowers.com"
FLIGHTS_ALIAS = "flights.flightpowers.com"
HOTELS_HOST = "hotels.flightpowers.com"

FLIGHT_TOOLS = ("search_oneway_flights", "search_roundtrip_flights")
HOTEL_TOOLS = ("search_hotels", "find_hotel_by_name", "compare_hotel_rates")


def make_settings(**overrides) -> Settings:
    base = dict(
        rapidapi_host="upstream.test",
        rapidapi_base_url="https://upstream.test",
        request_timeout_seconds=5.0,
        fallback_rapidapi_key=KEY,
        max_searches_per_tool_call=5,
        auto_max_searches=5,
        hub_requests_per_minute=0,
        fanout_deadline_seconds=0.0,
        max_concurrent_searches=3,
        max_http_connections=10,
        public_url="https://mcp.test/mcp",
        host="127.0.0.1",
        port=8000,
        log_path="",
        default_result_limit=10,
        signup_url="https://rapidapi.test/google-flights",
    )
    base.update(overrides)
    # The automatic raise is OFF unless a test asks for it: a helper
    # whose auto ceiling sat above the cap a test had just set would
    # quietly search more than the test said, which is how a cap
    # regression hides. Tests for the raise pass auto_max_searches.
    if "auto_max_searches" not in overrides:
        base["auto_max_searches"] = base["max_searches_per_tool_call"]
    return Settings(**base)


def build_with_upstream(handler, **overrides):
    server_module._shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return server_module.build_server(make_settings(**overrides))


ONEWAY_ROW = {
    "price": "$209",
    "price_as_number": 209,
    "duration": "3 hr 30 min",
    "duration_seconds": 12600,
    "airline": "Wizz Air",
    "stops": 0,
    "buy_link": "https://google.test/a",
    "departure_date": "2026-09-20",
    "departure_description": "6:50 PM on Sun, Sep 20",
    "arrival_description": "9:20 PM on Sun, Sep 20",
    "price_insights_low": 180,
    "price_insights_high": 330,
    "price_range_in_relation_to_other_periods": "low",
}


class TestTheResource:
    async def test_it_is_registered_once_with_the_app_mime_type(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            resources = await client.list_resources()
            assert [str(r.uri) for r in resources] == [WIDGET_URI]
            assert resources[0].mimeType == WIDGET_MIME_TYPE
            read = await client.read_resource(WIDGET_URI)
            assert read[0].text == FLIGHTS_WIDGET_HTML

    async def test_it_declares_no_external_origin(self):
        """The widget fetches nothing, so both hosts are told so
        explicitly (MCP Apps reads the CSP off `app=`, ChatGPT off
        `openai/widgetCSP`) rather than leaning on a host default that
        could widen later."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            meta = (await client.list_resources())[0].meta or {}
        assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
        assert meta["openai/widgetCSP"] == {
            "resource_domains": [],
            "connect_domains": [],
        }

    def test_the_served_bytes_are_self_contained_and_small(self):
        html = FLIGHTS_WIDGET_HTML
        assert len(html.encode()) < 40 * 1024, "the frame is over the 40 KB budget"
        lowered = html.lower()
        # No network of any kind: no origin, no fetch, no import.
        for banned in (
            "http://",
            "https://",
            "fetch(",
            "xmlhttprequest",
            "importscripts",
            "<img",
            "@import",
            "src=",
        ):
            assert banned not in lowered, f"{banned!r} would be an external request"
        # Every runtime value is written with textContent, never innerHTML:
        # `buy_link` and the airline names come from an upstream response.
        assert "innerhtml" not in lowered
        assert "textcontent" in lowered
        # Theme: light is the base, dark is defined for the host that tells
        # us nothing AND for the host that hands us a theme.
        assert "prefers-color-scheme: dark" in lowered
        assert '[data-theme="dark"]' in lowered
        assert "color-scheme: light dark" in lowered

    def test_the_frame_draws_what_the_card_promises(self):
        """The three things a fare card has to show, present as code
        rather than as an intention: the band with Google's verdict, the
        paired legs of a round trip, and a Book control per row."""
        html = FLIGHTS_WIDGET_HTML
        for token in (
            "price_insights_low",
            "price_insights_high",
            "price_range_in_relation_to_other_periods",
            "departure_flight_departure_description",
            "return_flight_departure_description",
            "total_price",
            "buy_link",
            "Book",
        ):
            assert token in html, f"{token} is not read by the widget"
        # Accessibility: the bar is an image with a described value and the
        # Book control is a real button, not a clickable div.
        assert 'role", "img"' in html or 'role="img"' in html
        assert 'el("button", "fp-book"' in html

    def test_only_the_first_cheapest_row_is_badged(self):
        """A date-range search routinely returns two identical fares (same
        carrier, two departure times). Badging both says "cheapest" twice,
        which is what the live preview payload did before this guard --
        TLV->BUD 2026-11-10 came back with $69 twice."""
        assert "var badged = false;" in FLIGHTS_WIDGET_HTML
        assert "&& !badged" in FLIGHTS_WIDGET_HTML

    def test_links_are_allowlisted_not_scheme_checked(self):
        """`buy_link` is upstream data and the host's link opener is the
        one place this frame could do real harm, so the gate is an
        allowlist, not "does it start with http". A scheme check would
        still have opened `https://evil.test/whatever` on a poisoned
        upstream row.

        The behaviour is executed against a hostile payload in
        tests/test_widget_render.py; this pins the shape for a machine
        with no browser."""
        html = FLIGHTS_WIDGET_HTML
        assert 'var LINK_HOSTS = ["google.com", "booking.com", "stay22.com"];' in html
        assert 'u.protocol !== "https:"' in html, "http: must be refused too"
        # A suffix match, so google.com.evil.test cannot pass as google.com.
        assert 'host === d || host.slice(-(d.length + 1)) === "." + d' in html
        # Both the opener AND the cell that draws the button use it, or a
        # dead button appears on a row whose link is refused.
        assert html.count("allowedLink(") >= 3

    def test_the_table_shows_ten_rows_and_scrolls_for_the_rest(self):
        """Matan, 2026-09-22, on the live card: "widget should not be that
        long: showcase the top 10, scrollable for more". A 36-fare answer
        used to render as a 36-row card.

        The behaviour is executed in tests/test_widget_render.py; this
        pins the shape for a machine with no browser."""
        html = FLIGHTS_WIDGET_HTML
        assert "var MAX_ROWS_SHOWN = 10;" in html
        assert 'scroll.setAttribute("data-capped", "1");' in html
        assert '.fp-scroll[data-capped="1"] { overflow-y: auto; }' in html
        # The column names survive the scroll, or row 20 is six unlabelled
        # cells.
        assert "position: sticky; top: 0; z-index: 1;" in html
        # The rest are reachable even on a host that swallows the inner
        # scroll and sizes the frame to its content.
        assert '"Show all " + total' in html
        assert '"Show top " + MAX_ROWS_SHOWN' in html

    def test_the_number_in_the_footer_is_measured_not_assumed(self):
        """"Showing 10 of 36" over eight visible rows would be a number we
        made up. The cap is measured off the layout (row 11's own position)
        and trimmed again if the card is still over its ceiling, so the
        count has to be counted afterwards rather than assumed to be 10."""
        html = FLIGHTS_WIDGET_HTML
        assert "rowEls[i].getBoundingClientRect().top - top" in html
        assert "var cap = span(Math.min(MAX_ROWS_SHOWN, n));" in html
        assert "if (rowEls[i].getBoundingClientRect().bottom > bottom + 0.5) break;" in html
        assert '"Showing " + shown + " of " + total' in html

    def test_destinations_become_pills_and_only_when_there_are_several(self):
        """Both tools take a LIST of destinations and answer with one flat
        `results` array. One destination gets no pills: a filter row with a
        single button is a control that cannot do anything."""
        html = FLIGHTS_WIDGET_HTML
        assert "function destOf(r) { return txt(r.to_airport); }" in html
        assert "if (buckets.length > 1) {" in html
        assert 'el("div", "fp-pills")' in html
        assert 'add("All", allRows).setAttribute("aria-pressed", "true")' in html
        assert 'wrap.setAttribute("aria-label", "Filter fares by destination")' in html
        # A destination called "constructor" is a destination like any
        # other, not a hit on Object.prototype.
        assert "var byDest = Object.create(null)" in html

    def test_picking_a_destination_recomputes_the_band_and_the_cheapest(self):
        """Google's price band is per route. Leaving Rome's band drawn over
        Athens' fares would be a made-up number on the one element of this
        card that claims to be Google's own tracking."""
        html = FLIGHTS_WIDGET_HTML
        assert "var band = bandOf(selected, cheapest, cheapestRow, routes);" in html
        assert (
            "renderTable(selected, isRoundTrip(selected), cheapest, routes > 1)"
            in html
        )
        assert "selected = picked;" in html

    def test_a_row_names_its_destination_only_while_several_are_shown(self):
        """Under All the rows are three routes interleaved by price, so
        each one says which. Under a pill, and on a single-destination
        answer, the column would be the same three letters all the way
        down and one more column squeezing the six that carry the fare."""
        html = FLIGHTS_WIDGET_HTML
        assert 'if (showDest) headers.splice(rt ? 2 : 1, 0, "To");' in html
        assert 'cell("To", raw ? destLabel(raw) : "", "", "fp-dest")' in html
        assert 'if (showDest) tr.appendChild(destCell(r));' in html
        assert "var routes = bucketsOf(selected).length;" in html

    def test_the_band_belongs_to_one_route_and_says_which(self):
        """Google tracks each route separately. A band averaged over
        three of them is a number nobody published, and a band taken from
        route A under a marker sitting on route B's fare is worse. So the
        band is the CHEAPEST route's own band, restricted to it -- no
        fallthrough to another route's numbers -- and the label stops
        saying "this route" while more than one is in the table."""
        html = FLIGHTS_WIDGET_HTML
        assert 'pool = rows.filter(function (r) { return destOf(r) === dest; });' in html
        assert '"Google price tracking · cheapest of " + routes + " routes"' in html
        assert '"Google price tracking for this route"' in html
        assert '", the route with the cheapest fare of these " + routes' in html

    def test_an_upstream_destination_string_cannot_widen_the_card(self):
        """`to_airport` is upstream data in a nowrap cell. A 400-character
        one measured a 3,370px sideways scroll at 760px, so the LABEL is
        capped -- and the full value is kept as the cell's title rather
        than lost."""
        html = FLIGHTS_WIDGET_HTML
        assert "var LABEL_MAX = 16;" in html
        assert 'out.slice(0, LABEL_MAX - 1).replace(/\\s+$/, "") + "…"' in html
        assert 'td.setAttribute("title", raw)' in html

    def test_a_fare_with_no_destination_is_still_reachable(self):
        """A row with no `to_airport` used to vanish under every pill.
        It is a fare the caller paid for, so it gets a bucket of its
        own."""
        html = FLIGHTS_WIDGET_HTML
        assert 'if (!d) { other.push(r); return; }' in html
        assert 'if (other.length) out.push({ dest: "", rows: other });' in html
        assert 'g.dest ? destLabel(g.dest) : "Other"' in html

    def test_an_unnamed_cheapest_fare_gets_no_band_rather_than_one_of_anothers(self):
        """Measured by the reviewer: a $90 row with no `to_airport`
        marked against another route's 250-420 band. With other routes in
        the table there is no band that belongs to this fare, so none is
        drawn. A table where NO row names a route keeps its band -- there
        is nothing to mix it with."""
        html = FLIGHTS_WIDGET_HTML
        assert "} else if (routes > 1) {\n      /*" in html
        assert html.count("      return null;\n    }") >= 1

    def test_a_narrow_frame_spends_its_height_on_fares(self):
        """At 380px the header, pills, band and footer ate 580 of the 780
        budget and the card answered a 36-fare search with two fares. The
        band collapses to one line below 520px, and five rows is a floor
        the ceiling gives way to."""
        html = FLIGHTS_WIDGET_HTML
        assert "var MIN_ROWS_SHOWN = 5;" in html
        assert 'window.matchMedia("(max-width: 519px)")' in html
        assert "if (isNarrow()) return renderBandLine(b, sym, routes);" in html
        assert 'el("div", "fp-bandline")' in html
        assert "var floor = Math.max(MIN_ROWS_PX, span(Math.min(MIN_ROWS_SHOWN, n)));" in html

    def test_ten_rows_are_fitted_too(self):
        """The old early return meant a 10-row answer was never capped at
        all: ten 184px block rows are a 2,029px card with no scroll and no
        Show all. The ceiling applies whatever the row count, and whether
        anything is hidden is decided by the fit, not by the count."""
        html = FLIGHTS_WIDGET_HTML
        assert "if (rowEls.length <= MAX_ROWS_SHOWN) return rowEls.length;" not in html
        assert "shown = fitRows(built.scroll, built.rowEls);\n        if (shown < total) {" in html

    def test_a_message_with_no_source_is_refused(self):
        """The tool result decides what this frame renders and what its
        buttons open. Executed in tests/test_widget_render.py."""
        assert "if (!ev.source || ev.source !== window.parent) return;" in FLIGHTS_WIDGET_HTML


class TestTheToolWiring:
    async def test_both_flights_tools_point_at_the_resource(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: (t.meta or {}) for t in await client.list_tools()}
        for name in FLIGHT_TOOLS:
            assert tools[name]["ui"]["resourceUri"] == WIDGET_URI
            assert tools[name]["ui"]["visibility"] == ["model"]
            # ChatGPT reads a different key for the same thing; without it
            # the card renders on claude.ai and nowhere else.
            assert tools[name]["openai/outputTemplate"] == WIDGET_URI

    async def test_the_hotels_tools_carry_none_of_it(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="both"
        )
        async with Client(mcp) as client:
            tools = {t.name: (t.meta or {}) for t in await client.list_tools()}
        for name in HOTEL_TOOLS:
            assert "ui" not in tools[name]
            assert "openai/outputTemplate" not in tools[name]

    async def test_a_hotels_deployment_registers_no_widget(self):
        """The resource is a flights surface. On the hotels hostname the
        flights tools are pruned, so a widget there would be an orphan
        pointing at tools that deployment does not have -- and a resource
        on a listing whose tools never reference it is a reviewer's
        question with no good answer."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(mcp) as client:
            assert await client.list_resources() == []
            for tool in await client.list_tools():
                assert "ui" not in (tool.meta or {})

    async def test_a_half_failed_registration_leaves_no_orphan(self, monkeypatch):
        """If the wiring fails AFTER the resource is registered, the
        resource has to come back out.

        Left behind, it would be a UI resource no tool points at and whose
        domain nothing rewrites -- a stale hash served to one of the two
        hostnames, which is precisely the silent failure this feature
        exists to avoid, on a listing that gets reviewed.
        """
        def boom(*_a, **_k):
            raise RuntimeError("middleware exploded")

        monkeypatch.setattr(server_module, "HostWidgetDomainMiddleware", boom)
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            assert await client.list_resources() == []
            for tool in await client.list_tools():
                assert "ui" not in (tool.meta or {})
                assert "openai/outputTemplate" not in (tool.meta or {})

    async def test_the_kill_switch_removes_every_trace(self):
        """`MCP_FLIGHTS_WIDGET=off` plus a redeploy is the rollback, and
        it has to leave the server exactly as it was -- not a resource
        nobody points at."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), flights_widget_enabled=False
        )
        async with Client(mcp) as client:
            assert await client.list_resources() == []
            for tool in await client.list_tools():
                assert "ui" not in (tool.meta or {})
                assert "openai/outputTemplate" not in (tool.meta or {})


class TestNothingChangesForANonUIClient:
    """The paying traffic is Cursor, Claude Code, scripts and Smithery,
    none of which render MCP UI. The widget is metadata on the tool
    DEFINITION; it must not put one byte into a tool RESULT."""

    @staticmethod
    def _upstream(_request):
        return httpx.Response(
            200, json=[ONEWAY_ROW], headers={"X-Search-Status": "ok"}
        )

    async def _search(self, **overrides):
        mcp = build_with_upstream(self._upstream, **overrides)
        async with Client(mcp) as client:
            return await client.call_tool(
                "search_oneway_flights",
                {
                    "from_airport": "TLV",
                    "to_airport": "BUD",
                    "departure_date": "2026-09-20",
                },
            )

    async def test_the_payload_is_identical_with_the_widget_on_and_off(self):
        with_widget = await self._search()
        without = await self._search(flights_widget_enabled=False)
        assert with_widget.structured_content == without.structured_content
        assert [c.text for c in with_widget.content] == [
            c.text for c in without.content
        ]

    async def test_no_widget_field_is_injected_into_the_rows(self):
        """The free server injects `book_label` and `widget_eyebrow` into
        the payload for its mapping-driven widget. This one computes both
        in JavaScript instead, precisely so the API surface does not
        move."""
        result = await self._search()
        row = result.structured_content["results"][0]
        assert set(row) == set(ONEWAY_ROW), "the widget changed a result row"
        for banned in ("book_label", "widget_eyebrow", "sponsored", "_meta"):
            assert banned not in result.structured_content

    async def test_the_result_carries_no_ui_meta(self):
        result = await self._search()
        meta = getattr(result, "meta", None) or {}
        assert "ui" not in meta


def _domain_of(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32] + ".claudemcpcontent.com"


class TestTheDomainHash:
    """`sha256(<connector URL>)[:32] + ".claudemcpcontent.com"`, and the
    connector URL is the one the USER added -- which differs per
    hostname on a deployment that answers on several."""

    def test_it_is_the_hash_claude_computes(self):
        url = "https://google-flights-mcp.flightpowers.com/mcp"
        assert claude_apps_domain(url) == _domain_of(url)

    @pytest.mark.parametrize(
        "raw",
        [
            "https://flights.flightpowers.com",
            "https://flights.flightpowers.com/",
            "https://flights.flightpowers.com/mcp",
            "https://flights.flightpowers.com/mcp/",
        ],
    )
    def test_a_bare_origin_and_a_trailing_slash_are_normalised(self, raw):
        """The 2026-09-15 incident, in one assertion. MCP_PUBLIC_URL was
        the bare origin; clients connect to `/mcp`; the two hashed
        differently and every widget on claude.ai was refused with a red
        chip and no log line."""
        assert (
            canonical_connector_url(raw) == "https://flights.flightpowers.com/mcp"
        )

    def test_a_non_url_is_handed_back_rather_than_guessed_at(self):
        assert canonical_connector_url("not-a-url") == "not-a-url"
        assert canonical_connector_url("") == ""

    class _Req:
        def __init__(self, headers, path="/mcp"):
            self.headers = headers
            self.url = type("U", (), {"path": path})()
            self.scope: dict = {}

    def test_the_connector_url_follows_the_host_the_caller_used(self):
        req = self._Req({"host": FLIGHTS_ALIAS})
        assert (
            connector_url_from_request(req, "https://fallback.test/mcp")
            == f"https://{FLIGHTS_ALIAS}/mcp"
        )

    def test_a_trailing_slash_on_the_request_path_is_dropped(self):
        req = self._Req({"host": FLIGHTS_HOST}, path="/mcp/")
        assert connector_url_from_request(req, "x").endswith("/mcp")

    def test_the_stashed_original_path_beats_the_rewritten_one(self):
        """`oauth.OAuthResourceGate` rewrites `/mcp/oauth` to `/mcp`
        before FastMCP sees the request, so `request.url.path` lies about
        what the user connected to. The scope key is the truth, and it
        wins. The end-to-end proof is
        `TestTheOAuthAliasDomain` below -- this pins the unit."""
        req = self._Req({"host": FLIGHTS_HOST}, path="/mcp")
        req.scope = {ORIGINAL_PATH_SCOPE_KEY: "/mcp/oauth"}
        assert (
            connector_url_from_request(req, "x")
            == f"https://{FLIGHTS_HOST}/mcp/oauth"
        )

    def test_an_absent_or_junk_stash_falls_back_to_the_request_path(self):
        req = self._Req({"host": FLIGHTS_HOST}, path="/mcp")
        req.scope = {}
        assert connector_url_from_request(req, "x") == f"https://{FLIGHTS_HOST}/mcp"
        req.scope = {ORIGINAL_PATH_SCOPE_KEY: ""}
        assert connector_url_from_request(req, "x") == f"https://{FLIGHTS_HOST}/mcp"
        req.scope = {ORIGINAL_PATH_SCOPE_KEY: 7}
        assert connector_url_from_request(req, "x") == f"https://{FLIGHTS_HOST}/mcp"

    def test_a_forwarded_host_is_read_only_when_host_is_absent(self):
        both = self._Req({"host": FLIGHTS_HOST, "x-forwarded-host": "evil.test"})
        assert FLIGHTS_HOST in connector_url_from_request(both, "x")
        only = self._Req({"x-forwarded-host": f"{FLIGHTS_ALIAS}, proxy.internal"})
        assert connector_url_from_request(only, "x") == f"https://{FLIGHTS_ALIAS}/mcp"

    def test_no_host_falls_back_to_the_canonical_url(self):
        assert (
            connector_url_from_request(self._Req({}), "https://fallback.test/mcp")
            == "https://fallback.test/mcp"
        )
        assert connector_url_from_request(None, "https://f.test/mcp") == (
            "https://f.test/mcp"
        )

    def test_localhost_is_http(self):
        req = self._Req({"host": "localhost:8000"})
        assert connector_url_from_request(req, "x") == "http://localhost:8000/mcp"

    def test_the_middleware_leaves_a_foreign_domain_alone(self):
        """Only a value that already looks like an MCP Apps content
        domain is ours to rewrite."""
        mw = HostWidgetDomainMiddleware("https://mcp.test/mcp")
        assert mw._retarget({"ui": {"domain": "example.com"}}, "x.claudemcpcontent.com") is None
        assert mw._retarget({"openai/widgetCSP": {}}, "x") is None
        assert mw._retarget(None, "x") is None
        changed = mw._retarget(
            {"ui": {"domain": "old.claudemcpcontent.com", "csp": {}}, "k": 1},
            "new.claudemcpcontent.com",
        )
        assert changed["ui"]["domain"] == "new.claudemcpcontent.com"
        assert changed["ui"]["csp"] == {} and changed["k"] == 1


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
        "clientInfo": {"name": "widget-test", "version": "1.0"},
    },
}
MCP_ENV = (
    "MCP_PRODUCTS",
    "MCP_PRODUCTS_BY_HOST",
    "MCP_PUBLIC_URL",
    "MCP_PUBLIC_URL_FLIGHTS",
    "MCP_PUBLIC_URL_HOTELS",
    "MCP_FLIGHTS_WIDGET",
    "RAPIDAPI_KEY",
)


def _payload(body: str) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in {body!r}")


@pytest.fixture
def combined(monkeypatch):
    """The production shape: one process, three hostnames."""
    for name in MCP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", "flights")
    monkeypatch.setenv("MCP_PUBLIC_URL_FLIGHTS", "https://flights.golden.test/mcp")
    monkeypatch.setenv("MCP_PUBLIC_URL_HOTELS", "https://hotels.golden.test/mcp")
    return entrypoint.build_entrypoint().app


async def _rpc(app, host, request_body, query=""):
    headers = dict(MCP_HEADERS)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://{host}"
        ) as client:
            init = await client.post(
                "/mcp" + query, json=INITIALIZE, headers=headers
            )
            session = init.headers.get("mcp-session-id")
            if session:
                headers["mcp-session-id"] = session
            response = await client.post(
                "/mcp" + query, json=request_body, headers=headers
            )
    return _payload(response.text)


class TestThePerHostDomainOverHTTP:
    """The whole point of the middleware, driven the way a host drives
    the app. `resources/list` AND `resources/read`: claude.ai validates
    on the READ, and the free server shipped a version that rewrote only
    the list -- the alias population kept the red chip."""

    @pytest.mark.parametrize("host", [FLIGHTS_HOST, FLIGHTS_ALIAS])
    async def test_list_and_read_carry_this_hosts_domain(self, combined, host):
        expected = _domain_of(f"https://{host}/mcp")

        listed = await _rpc(
            combined, host, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}
        )
        resources = listed["result"]["resources"]
        assert [r["uri"] for r in resources] == [WIDGET_URI]
        assert resources[0]["_meta"]["ui"]["domain"] == expected

        read = await _rpc(
            combined,
            host,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": WIDGET_URI},
            },
        )
        contents = read["result"]["contents"]
        assert contents[0]["_meta"]["ui"]["domain"] == expected
        assert contents[0]["text"] == FLIGHTS_WIDGET_HTML

    async def test_the_two_hostnames_get_different_domains(self, combined):
        """One baked-in value would be right for exactly one of them, and
        silently broken -- no error, no log -- for the other."""
        a = await _rpc(
            combined,
            FLIGHTS_HOST,
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        )
        b = await _rpc(
            combined,
            FLIGHTS_ALIAS,
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        )
        da = a["result"]["resources"][0]["_meta"]["ui"]["domain"]
        db = b["result"]["resources"][0]["_meta"]["ui"]["domain"]
        assert da != db
        assert da == _domain_of(f"https://{FLIGHTS_HOST}/mcp")
        assert db == _domain_of(f"https://{FLIGHTS_ALIAS}/mcp")

    async def test_the_hotels_hostname_serves_no_resource(self, combined):
        listed = await _rpc(
            combined, HOTELS_HOST, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}
        )
        assert listed["result"]["resources"] == []

    async def test_a_gateway_request_is_unaffected(self, combined):
        """Smithery proxies calls as `?api_key=<its own key>&config=<b64>`
        and terminates the connection itself, so the Host it sends is not
        ours. The resource still lists, the domain is still a valid hash
        of what that caller connected to, and the tool surface does not
        move -- a gateway user renders no UI and must lose nothing."""
        query = "?api_key=smithery-own-key&config=eyJyYXBpZGFwaV9rZXkiOiJ4In0="
        listed = await _rpc(
            combined,
            FLIGHTS_HOST,
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            query=query,
        )
        domain = listed["result"]["resources"][0]["_meta"]["ui"]["domain"]
        assert domain.endswith(".claudemcpcontent.com")
        assert domain == _domain_of(f"https://{FLIGHTS_HOST}/mcp")

        tools = await _rpc(
            combined,
            FLIGHTS_HOST,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            query=query,
        )
        assert [t["name"] for t in tools["result"]["tools"]] == list(FLIGHT_TOOLS)


# ── the /mcp/oauth alias, end to end ─────────────────────────────────────
#
# The gate rewrites `/mcp/oauth` to `/mcp` before FastMCP sees the request,
# so until the scope stash existed, a connector added on the alias -- the URL
# in every printed guide and in every connector added before 2026-09-09 --
# was served the hash of `.../mcp` while claude.ai computed the hash of
# `.../mcp/oauth`. The card would simply never have rendered for them, with
# no error anywhere. A fake request object cannot catch that: the rewrite
# happens in a layer a fake request never passes through. So this drives the
# real ASGI stack with a real granted bearer.

from tests.test_oauth import (  # noqa: E402
    MCP_HEADERS as OAUTH_MCP_HEADERS,
    MCP_OAUTH_PATH,
    ORIGIN,
    Session,
    _build,
    _granted_access_token,
    _payload as _oauth_payload,
)


@pytest.fixture
def live_oauth(monkeypatch):
    """One flights deployment with MCP OAuth configured, on `mcp.test`."""
    return _build(monkeypatch)


async def _resource_domain(http, path, method, bearer, uri=WIDGET_URI):
    """`_meta.ui.domain` off `resources/list` or `resources/read` at `path`."""
    headers = {**OAUTH_MCP_HEADERS, "authorization": f"Bearer {bearer}"}
    started = await http.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "widget-test", "version": "1.0"},
            },
        },
        headers=headers,
    )
    assert started.status_code == 200, started.text
    session = started.headers.get("mcp-session-id")
    if session:
        headers["mcp-session-id"] = session
    params = {"uri": uri} if method == "resources/read" else {}
    response = await http.post(
        path,
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = _oauth_payload(response.text)["result"]
    node = (
        result["contents"][0]
        if method == "resources/read"
        else result["resources"][0]
    )
    return node["_meta"]["ui"]["domain"]


class TestTheOAuthAliasDomain:
    """Whatever URL the user connected to is what gets hashed -- including
    the one the gate rewrites away."""

    @pytest.mark.parametrize("method", ["resources/list", "resources/read"])
    async def test_the_alias_hashes_itself_not_the_rewritten_path(
        self, live_oauth, method
    ):
        async with Session(live_oauth) as session:
            bearer = await _granted_access_token(session.http)
            domain = await _resource_domain(
                session.http, MCP_OAUTH_PATH, method, bearer
            )
        assert domain == _domain_of(f"{ORIGIN}{MCP_OAUTH_PATH}")
        # The bug this exists for: the rewritten path's hash.
        assert domain != _domain_of(f"{ORIGIN}/mcp")

    @pytest.mark.parametrize("method", ["resources/list", "resources/read"])
    async def test_plain_mcp_still_hashes_plain_mcp(self, live_oauth, method):
        """The stash must not leak the other way: a caller on `/mcp` with
        the same token gets `/mcp`."""
        async with Session(live_oauth) as session:
            bearer = await _granted_access_token(session.http)
            domain = await _resource_domain(session.http, "/mcp", method, bearer)
        assert domain == _domain_of(f"{ORIGIN}/mcp")
        assert domain != _domain_of(f"{ORIGIN}{MCP_OAUTH_PATH}")
