"""
Server behaviour.

Tools are exercised through FastMCP's in-memory Client rather than over HTTP,
so the assertions are about our logic and not FastMCP's transport. That also
means `get_http_request()` raises inside the tools, which is the real-world
stdio case and exercises the env-fallback credential branch. The HTTP-shaped
concern that matters -- pulling a key out of headers and query params -- is
covered in test_credentials.py against the pure function.
"""

import json

import httpx
import pytest
from fastmcp import Client

from src.rapidapi_client import AuthError, QuotaError
from src.server import _dedupe, _row_sort_key, _usage_block, build_server
from src.settings import HARD_MAX_SEARCHES, Settings

KEY = "test-key-that-is-long-enough-to-pass"


def make_settings(**overrides) -> Settings:
    base = dict(
        rapidapi_host="upstream.test",
        rapidapi_base_url="https://upstream.test",
        request_timeout_seconds=5.0,
        # Set so tools resolve a credential without an HTTP request in scope.
        # Production leaves this empty; see test_settings.py.
        fallback_rapidapi_key=KEY,
        max_searches_per_tool_call=5,
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
    return Settings(**base)


def build_with_upstream(handler, **overrides):
    """A server whose shared HTTP client is a MockTransport."""
    import src.server as server_module

    server_module._shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return build_server(make_settings(**overrides))


ONEWAY_ROW = {
    "price": "$209",
    "price_as_number": 209,
    "duration_seconds": 12600,
    "airline": "Wizz Air",
    "stops": 0,
    "buy_link": "https://google.test/a",
    "departure_date": "2026-09-20",
}


async def call(mcp, tool: str, **kwargs):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, kwargs)
        return result.structured_content


async def call_result(mcp, tool: str, **kwargs):
    """The whole CallToolResult, errors included.

    `call` cannot reach a degraded search any more: that result now carries
    `isError: true`, and the client raises on it by default. Anything
    asserting on the error flag, or on a payload that rides alongside it,
    has to look at the result rather than just the structured content.
    """
    async with Client(mcp) as client:
        return await client.call_tool(tool, kwargs, raise_on_error=False)


class TestToolRegistration:
    """Asserted over the wire via Client, not against server internals.

    What a registry reviewer and a model both see is the MCP `tools/list`
    payload, so that is what these check -- and it stays valid across FastMCP
    releases that rename internal accessors.
    """

    @pytest.mark.asyncio
    async def test_all_four_tools_exist(self):
        """Both products on one server. The same RapidAPI key scopes access
        per subscription, so a flights-only caller simply gets a 403 from the
        hotel tools rather than needing a second credential."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == {
            "search_oneway_flights",
            "search_roundtrip_flights",
            "search_hotels",
            "find_hotel_by_name",
        }

    @pytest.mark.asyncio
    async def test_tool_names_are_verb_noun(self):
        """MCP Growth Playbook tactic 4: the model picks tools by reading
        names, so they are ad copy with an audience of one literal reader."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                # find_ reads better than search_ for a single named property,
                # and the point of the rule is that the verb states the action.
                assert tool.name.startswith(("search_", "find_"))

    @pytest.mark.asyncio
    async def test_descriptions_state_inputs_outputs_and_the_key(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                text = tool.description or ""
                assert "Input:" in text
                assert "Returns" in text
                # Every tool has to say the result goes stale, because a model
                # reusing a cached fare or rate is the failure mode that makes
                # the whole product look wrong.
                assert "stale" in text or "RapidAPI key" in text

    @pytest.mark.asyncio
    async def test_no_ad_machinery_is_registered(self):
        """The whole listing strategy rests on this server carrying no ads.
        The free server registers ad widgets as resources; a resource showing
        up here would mean one crept back in."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            assert await client.list_resources() == []

    @pytest.mark.asyncio
    async def test_sort_type_is_not_exposed(self):
        """Upstream ordering is per-search, and this server merges up to `cap`
        searches and re-sorts the merged set via `sort_by` -- so a passed-through
        `sort_type` would be a parameter whose effect the merge discards."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                assert "sort_type" not in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_range_and_multi_destination_are_exposed(self):
        """The capabilities the generated passthrough lacks, and the reason
        one user intent is one billed fan-out here instead of thirty calls."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}

        oneway = tools["search_oneway_flights"].inputSchema["properties"]
        assert "departure_date_from" in oneway
        assert "departure_date_to" in oneway
        assert "use_fallback" in oneway
        assert "limit" in oneway
        assert "nights" in tools["search_roundtrip_flights"].inputSchema["properties"]


class TestMissingCredential:
    @pytest.mark.asyncio
    async def test_no_key_spends_nothing_and_explains(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(200, json=[ONEWAY_ROW])

        mcp = build_with_upstream(handler, fallback_rapidapi_key="")
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )

        assert out["needs_api_key"] is True
        assert out["results"] == []
        assert out["signup_url"] == "https://rapidapi.test/google-flights"
        # The point of the early return: a keyless call must cost nothing.
        assert calls["n"] == 0


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_returns_rows_and_reports_spend(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                200,
                json=[ONEWAY_ROW],
                headers={
                    "x-ratelimit-requests-limit": "1000",
                    "x-ratelimit-requests-remaining": "993",
                },
            )
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )

        assert out["result_count"] == 1
        assert out["api_usage"]["requests_used_by_this_call"] == 1
        assert out["api_usage"]["plan_requests_remaining"] == 993

    @pytest.mark.asyncio
    async def test_a_date_range_is_one_call_and_reports_the_true_cost(self):
        """The capability that justifies this server over a passthrough, and
        the number a user must see before their invoice shows it."""
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(200, json=[dict(ONEWAY_ROW, buy_link=f"u{calls['n']}")])

        mcp = build_with_upstream(handler)
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-20",
            departure_date_to="2026-09-22",
        )

        assert calls["n"] == 3
        assert out["api_usage"]["requests_used_by_this_call"] == 3
        assert "3 of your RapidAPI plan's requests" in out["api_usage"]["note"]

    @pytest.mark.asyncio
    async def test_truncation_is_reported_not_hidden(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), max_searches_per_tool_call=3
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-30",
        )
        coverage = out["search_coverage"]
        assert coverage["truncated"] is True
        assert coverage["searched_combinations"] == 3
        assert coverage["requested_combinations"] == 30

    @pytest.mark.asyncio
    async def test_max_searches_lowers_the_cap(self):
        """The user controlling their own spend is the point of this knob."""
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, max_searches_per_tool_call=10)
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-30",
            max_searches=2,
        )
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_max_searches_cannot_raise_the_cap(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, max_searches_per_tool_call=3)
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-30",
            max_searches=99,
        )
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_empty_result_is_answered_not_raised(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="XXX",
            departure_date="2026-09-20",
        )
        assert out["result_count"] == 0
        assert "No flights were found" in out["message"]


class TestAccountFailures:
    @pytest.mark.asyncio
    async def test_auth_failure_becomes_an_actionable_answer(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                403, json={"message": "You are not subscribed to this API."}
            )
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )

        assert out["needs_api_key"] is True
        assert "not subscribed" in out["message"]
        assert out["signup_url"] in out["message"]

    @pytest.mark.asyncio
    async def test_auth_failure_across_a_fanout_is_one_fact_not_many(self):
        """Fifteen failed searches for one bad key must not read as fifteen
        independent outages."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(401, json={"message": "Invalid API key"})
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-05",
        )
        assert out["needs_api_key"] is True
        assert "temporarily unavailable" not in out.get("message", "")

    @pytest.mark.asyncio
    async def test_quota_exhaustion_says_how_to_fix_it(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                429,
                json={"message": "You have exceeded the MONTHLY quota"},
                headers={"x-ratelimit-requests-remaining": "0"},
            )
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )

        assert out["quota_exhausted"] is True
        assert "narrowing the range" in out["message"]
        assert out["api_usage"]["plan_requests_remaining"] == 0

    @pytest.mark.asyncio
    async def test_total_upstream_outage_raises(self):
        """Distinct from an empty result: nothing was learned, so answering
        'no flights' would be a lie."""
        from fastmcp.exceptions import ToolError

        mcp = build_with_upstream(lambda _r: httpx.Response(500, text="boom"))
        with pytest.raises(ToolError, match="temporarily unavailable"):
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="TLV",
                to_airport="BUD",
                departure_date="2026-09-20",
            )

    @pytest.mark.asyncio
    async def test_partial_failure_still_answers(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            # 422 rather than 500: a server fault would be retried and would
            # then succeed, leaving nothing partial to assert on.
            if calls["n"] == 1:
                return httpx.Response(422, json={"detail": "bad combo"})
            return httpx.Response(200, json=[dict(ONEWAY_ROW, buy_link=f"u{calls['n']}")])

        mcp = build_with_upstream(handler)
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-03",
        )
        assert out["result_count"] >= 1
        assert "partial" in out


class TestValidation:
    @pytest.mark.asyncio
    async def test_bad_sort_by_is_rejected(self):
        from fastmcp.exceptions import ToolError

        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        with pytest.raises(ToolError, match="sort_by"):
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="TLV",
                to_airport="BUD",
                departure_date="2026-09-20",
                sort_by="cheapest",
            )

    @pytest.mark.asyncio
    async def test_roundtrip_needs_a_return_or_nights(self):
        from fastmcp.exceptions import ToolError

        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        with pytest.raises(ToolError, match="return_date"):
            await call(
                mcp,
                "search_roundtrip_flights",
                from_airport="TLV",
                to_airport="BUD",
                departure_date="2026-09-20",
            )

    @pytest.mark.asyncio
    async def test_planning_failure_costs_nothing(self):
        from fastmcp.exceptions import ToolError

        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler)
        with pytest.raises(ToolError):
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="TLV",
                to_airport="BUD",
                departure_date="not-a-date",
            )
        assert calls["n"] == 0


class TestHelpers:
    def test_dedupe_keys_on_buy_link(self):
        rows = [{"buy_link": "a"}, {"buy_link": "a"}, {"buy_link": "b"}]
        assert len(_dedupe(rows)) == 2

    def test_dedupe_keeps_rows_without_a_link(self):
        rows = [{"price": 1}, {"price": 2}]
        assert len(_dedupe(rows)) == 2

    def test_sort_puts_missing_prices_last(self):
        rows = [{"price_as_number": None}, {"price_as_number": 100}]
        ordered = sorted(rows, key=_row_sort_key("price"))
        assert ordered[0]["price_as_number"] == 100

    def test_sort_falls_back_to_roundtrip_keys(self):
        rows = [{"total_price_as_number": 300}, {"total_price_as_number": 100}]
        ordered = sorted(rows, key=_row_sort_key("price"))
        assert ordered[0]["total_price_as_number"] == 100

    def test_usage_block_without_quota_headers_still_states_the_cost(self):
        block = _usage_block(7, {})
        assert block["requests_used_by_this_call"] == 7
        assert "7 of your RapidAPI plan's requests" in block["note"]

    def test_usage_block_explains_what_a_request_is(self):
        """The whole point: a user surprised by fifteen requests churns."""
        assert "one billed request" in _usage_block(1, {})["note"]


class TestDirectoryConformance:
    """Metadata both app directories check at review time.

    Anthropic's review requires every tool to carry a title and the applicable
    read-only / destructive hint; OpenAI lists missing annotations as a common
    rejection reason. A tool with no annotations is treated as potentially
    destructive, which is exactly wrong for two search tools -- and the failure
    is a rejected submission weeks later, not an error now.
    """

    @pytest.mark.asyncio
    async def test_every_tool_has_a_title(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                assert tool.title, f"{tool.name} has no title"

    @pytest.mark.asyncio
    async def test_every_tool_is_annotated_read_only(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                ann = tool.annotations
                assert ann is not None, f"{tool.name} has no annotations"
                assert ann.readOnlyHint is True
                assert ann.destructiveHint is False

    @pytest.mark.asyncio
    async def test_tools_declare_an_open_world(self):
        """They reach a live third-party API, not a closed domain."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                assert tool.annotations.openWorldHint is True

    @pytest.mark.asyncio
    async def test_tools_are_not_claimed_idempotent(self):
        """Fares move between two identical calls; claiming idempotency would
        invite a host to cache or dedupe a search that must stay live."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                assert tool.annotations.idempotentHint is False


class TestPrompts:
    """Prompts are what the USER picks in the client, and they are also where
    the three usage rules live that a model otherwise infers badly: one call
    with a range, judge the fare against the price band, never reuse a stale
    fare. If a prompt stops carrying those, the server gets worse answers."""

    EXPECTED = {
        "cheapest_dates",
        "compare_destinations",
        "plan_trip",
        "is_this_fare_good",
    }

    @pytest.mark.asyncio
    async def test_all_prompts_registered(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            names = {p.name for p in await client.list_prompts()}
        assert self.EXPECTED <= names

    @pytest.mark.asyncio
    async def test_every_prompt_has_a_title_and_description(self):
        """Without both, a client has nothing to render in its picker."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for p in await client.list_prompts():
                assert p.title, f"{p.name} has no title"
                assert p.description, f"{p.name} has no description"

    @pytest.mark.asyncio
    async def test_range_prompts_forbid_one_call_per_date(self):
        """The single most expensive mistake a model makes with this server."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            for name, args in (
                ("cheapest_dates", {"from_airport": "TLV", "to_airport": "BUD",
                                    "month": "October 2026"}),
                ("compare_destinations", {"from_airport": "TLV",
                                          "destinations": "BUD,VIE",
                                          "when": "in October"}),
            ):
                r = await client.get_prompt(name, args)
                text = " ".join(
                    m.content.text for m in r.messages if hasattr(m.content, "text")
                )
                assert "ONCE" in text
                assert "search_coverage" in text

    @pytest.mark.asyncio
    async def test_prompts_ask_for_the_price_verdict_not_a_bare_number(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            r = await client.get_prompt(
                "is_this_fare_good",
                {"from_airport": "TLV", "to_airport": "BUD",
                 "departure_date": "2026-09-20"},
            )
            text = " ".join(
                m.content.text for m in r.messages if hasattr(m.content, "text")
            )
        assert "price_range_in_relation_to_other_periods" in text
        assert "fresh lookup" in text

    async def _plan_trip_text(self, mcp, **extra):
        args = {"from_airport": "TLV", "to_airport": "FCO", "when": "in May"}
        args.update(extra)
        async with Client(mcp) as client:
            r = await client.get_prompt("plan_trip", args)
        return " ".join(
            m.content.text for m in r.messages if hasattr(m.content, "text")
        )

    @pytest.mark.asyncio
    async def test_omitted_budget_leaves_no_dangling_clause(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        text = await self._plan_trip_text(mcp)
        assert "Keep the total under" not in text
        assert "nights." in text

    @pytest.mark.asyncio
    async def test_supplied_budget_is_carried_into_the_prompt(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        text = await self._plan_trip_text(mcp, budget="$400")
        assert "Keep the total under $400" in text


class TestProductSelection:
    """One codebase, three deployments.

    flights.flightpowers.com and hotels.flightpowers.com each sell one
    product. A subscriber must never be shown tools their key can only 403 on,
    so this is asserted per mode rather than trusted.
    """

    @pytest.mark.asyncio
    async def test_default_is_both(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert len(names) == 4

    @pytest.mark.asyncio
    async def test_flights_only_hides_hotel_tools(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="flights"
        )
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == {"search_oneway_flights", "search_roundtrip_flights"}

    @pytest.mark.asyncio
    async def test_hotels_only_hides_flight_tools(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == {"search_hotels", "find_hotel_by_name"}

    @pytest.mark.asyncio
    async def test_hotels_only_registers_no_prompts(self):
        """All four prompts are flight questions -- on a hotels deployment
        they would be dead entries in the client's prompt picker."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(mcp) as client:
            assert await client.list_prompts() == []

    @pytest.mark.asyncio
    async def test_flights_keeps_its_prompts(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="flights"
        )
        async with Client(mcp) as client:
            assert len(await client.list_prompts()) == 4

    def test_a_typo_is_rejected_rather_than_failing_open(self):
        """"hotel" quietly serving both products is the failure nobody
        notices until a customer reports a broken tool."""
        import os

        from src.settings import load_settings

        prev = os.environ.get("MCP_PRODUCTS")
        os.environ["MCP_PRODUCTS"] = "hotel"
        try:
            with pytest.raises(RuntimeError, match="MCP_PRODUCTS"):
                load_settings()
        finally:
            if prev is None:
                os.environ.pop("MCP_PRODUCTS", None)
            else:
                os.environ["MCP_PRODUCTS"] = prev

    def test_health_names_the_right_product(self):
        """A hotels-only deployment reporting "google-flights-mcp" to every
        registry that polls /health is a small lie in a visible place."""
        from src.server import service_name

        assert service_name("flights") == "google-flights-mcp"
        assert service_name("hotels") == "booking-hotels-mcp"
        assert service_name("both") == "flightpowers-travel-mcp"


class TestSearchStatusIsBelieved:
    """The backend answers a failed scrape and a genuine empty with the same
    HTTP 200 and the same `[]`, and says which in `X-Search-Status`. Until that
    header was read, the tool answered both with "No flights were found ... try
    a different date" -- a confident, checkable, wrong claim about the world
    that a model repeats to the user as fact.
    """

    @pytest.mark.asyncio
    async def test_a_degraded_search_is_not_reported_as_no_flights(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                200,
                json=[],
                headers={"X-Search-Status": "degraded", "X-Search-Reason": "blocked_page"},
            )
        )
        result = await call_result(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        # The failure is on the result itself, not only inside the payload.
        assert result.is_error
        out = result.structured_content
        assert out["result_count"] == 0
        assert out["search_status"] == "degraded"
        assert "No flights were found" not in out["message"]
        assert "did not complete" in out["message"]
        assert "blocked_page" in out["message"]

    @pytest.mark.asyncio
    async def test_the_degraded_message_tells_the_model_what_not_to_say(self):
        """The only consumer of this field is a language model, and it will act
        on it. Naming the wrong action is worth the words."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[], headers={"X-Search-Status": "degraded"})
        )
        result = await call_result(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        assert result.is_error
        out = result.structured_content
        assert "do not tell the user that none exist" in out["message"].lower()

    @pytest.mark.asyncio
    async def test_a_genuine_empty_still_says_no_flights(self):
        """The old message was not wrong, only unconditional. When the backend
        says the search completed, it is exactly right."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[], headers={"X-Search-Status": "empty"})
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        assert out["search_status"] == "empty"
        assert "No flights were found" in out["message"]

    @pytest.mark.asyncio
    async def test_a_silent_backend_does_not_get_the_benefit_of_the_doubt(self):
        """A backend that predates the header, or a hop that dropped it. The
        result is unchanged from today -- but no `search_status` is claimed,
        because we do not know one."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        assert "search_status" not in out
        assert "No flights were found" in out["message"]

    @pytest.mark.asyncio
    async def test_a_healthy_search_is_labelled_ok(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                200, json=[ONEWAY_ROW], headers={"X-Search-Status": "ok"}
            )
        )
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        assert out["search_status"] == "ok"
        assert out["result_count"] == 1
        assert "partial" not in out

    @pytest.mark.asyncio
    async def test_one_degraded_date_in_a_range_is_not_hidden_by_the_others(self):
        """A fan-out is many independent searches. Recording the outcome as one
        fact per request -- last writer wins, the way quota is recorded -- would
        let a healthy date bury a failed one and quietly under-report the range.
        """
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, json=[], headers={"X-Search-Status": "degraded"}
                )
            return httpx.Response(
                200, json=[ONEWAY_ROW], headers={"X-Search-Status": "ok"}
            )

        mcp = build_with_upstream(handler)
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-03",
        )
        assert out["result_count"] > 0
        assert out["search_status"] == "partial"
        assert "did not complete" in out["partial"]

    @pytest.mark.asyncio
    async def test_an_incomplete_note_does_not_replace_the_failure_note(self):
        """Both can be true at once: one combination raised, another answered
        with a degraded empty. Losing either would understate the gap."""
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, json=[], headers={"X-Search-Status": "degraded"}
                )
            if calls["n"] == 2:
                return httpx.Response(422, json={"message": "bad payload"})
            return httpx.Response(
                200, json=[ONEWAY_ROW], headers={"X-Search-Status": "ok"}
            )

        mcp = build_with_upstream(handler)
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date_from="2026-09-01",
            departure_date_to="2026-09-03",
        )
        assert "searches failed" in out["partial"]
        assert "did not complete" in out["partial"]

    @pytest.mark.asyncio
    async def test_roundtrip_reports_the_same_way(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[], headers={"X-Search-Status": "degraded"})
        )
        result = await call_result(
            mcp,
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
            return_date="2026-09-27",
        )
        assert result.is_error
        out = result.structured_content
        assert out["search_status"] == "degraded"
        assert "No flights were found" not in out["message"]


class TestFallbackDefault:
    """`use_fallback` is tri-state upstream, and the default has to be absent.

    The backend reads the field as: true = run the fallback client inline on
    every attempt; false = never, last-resort escalation included; absent =
    escalate to it once, only after every retry for a combination has failed.
    These tools used to declare `use_fallback: bool = False`, which sent an
    explicit `false` on every call and excluded our own users from the
    escalation -- the exact opposite of the intent.
    """

    @staticmethod
    def _recording_upstream():
        bodies: list[dict] = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=[ONEWAY_ROW])

        return bodies, build_with_upstream(handler)

    @pytest.mark.asyncio
    async def test_default_call_omits_the_key_entirely(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
        )
        assert bodies, "the tool made no upstream call"
        assert "use_fallback" not in bodies[0]

    @pytest.mark.asyncio
    async def test_roundtrip_default_omits_it_too(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
            return_date="2026-09-27",
        )
        assert bodies
        assert "use_fallback" not in bodies[0]

    @pytest.mark.asyncio
    async def test_an_explicit_choice_still_reaches_the_backend(self):
        """Absent is only the default; a caller who states a value keeps it."""
        for requested in (True, False):
            bodies, mcp = self._recording_upstream()
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="TLV",
                to_airport="BUD",
                departure_date="2026-09-20",
                use_fallback=requested,
            )
            assert bodies[0]["use_fallback"] is requested

    @pytest.mark.asyncio
    async def test_the_schema_offers_null_and_defaults_to_it(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}

        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            schema = tools[name].inputSchema
            prop = schema["properties"]["use_fallback"]
            assert prop["default"] is None
            assert {"type": "null"} in prop["anyOf"]
            assert "use_fallback" not in schema.get("required", [])

    @pytest.mark.asyncio
    async def test_the_model_is_told_what_the_flag_actually_does(self):
        """FastMCP does not lift a docstring `Args:` entry into the schema, and
        these tools pass an explicit `description=` which overrides the
        docstring anyway -- so a parameter the model must reason about has to
        carry a `Field(description=...)`. The old text ("Wait longer on hard
        routes") was also simply wrong: it does not wait, it re-runs the search
        through a different client."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}

        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            text = tools[name].inputSchema["properties"]["use_fallback"]["description"]
            assert "Wait longer" not in text
            assert "Leave unset" in text
            assert "can time out" in text

    @pytest.mark.asyncio
    async def test_an_empty_result_does_not_recommend_the_inline_path(self):
        """Inline `true` hangs until the function's ``Timeout``, so the
        message a model reads on an empty result must not point at it."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        out = await call(
            mcp,
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="XXX",
            departure_date="2026-09-20",
        )
        assert "set use_fallback to true" not in out["message"]


class TestServerInstructions:
    """The one paragraph every client reads before it calls anything.

    hotels.flightpowers.com shipped with the flights text -- it opened
    "Real-time Google Flights search" to every hotels client at connect
    time. A model that is told the wrong thing at handshake either calls the
    server wrong or does not reach for it at all, and nothing downstream
    corrects it. These assertions are per product, because the bug was
    exactly one deployment inheriting another's prose.
    """

    # A real per-product signup URL, because the fixture's default carries
    # "google-flights" in the path and would smuggle the word "flight" into
    # a hotels string the test is checking is flight-free.
    SIGNUP = {
        "flights": "https://rapidapi.test/google-flights",
        "hotels": "https://rapidapi.test/booking",
        "both": "https://rapidapi.test/google-flights",
    }

    def _settings(self, products: str):
        return make_settings(products=products, signup_url=self.SIGNUP[products])

    def _instructions(self, products: str) -> str:
        from src.server import build_instructions

        return build_instructions(self._settings(products))

    # The service is called FlightPowers, and since the naming edit that word
    # opens the hotels string too, so a bare `"flight" not in text` now trips
    # on the brand instead of on flight-product prose. The brand is stripped
    # before the scan; the sentence this test was written for ("Real-time
    # Google Flights search"), the word "flights", and the flight tool names
    # all still trip it.
    @staticmethod
    def _without_the_brand(text: str) -> str:
        return text.replace("FlightPowers", "").replace("flightpowers", "")

    def test_hotels_deployment_does_not_call_itself_a_flights_server(self):
        text = self._instructions("hotels")
        assert "Google Flights" not in text
        assert "flight" not in self._without_the_brand(text).lower()
        assert "hotel" in text.lower()

    def test_hotels_names_both_of_its_tools(self):
        text = self._instructions("hotels")
        assert "search_hotels" in text
        assert "find_hotel_by_name" in text

    def test_hotels_sells_the_per_country_pricing(self):
        """`price_as_seen_from` is the capability rivals do not have, and it
        is the reason a business buyer rather than a hobbyist subscribes.

        The instructions sell it as a repeat-sampled per-country view -- one
        property and its dates held fixed, each country called a few times --
        not as a one-shot country comparison (#381). Match case-insensitively
        so a copy edit that starts the sentence with "Rate parity" does not
        fail a test about the claim being present.
        """
        text = self._instructions("hotels")
        assert "price_as_seen_from" in text
        assert "rate parity" in text.lower()

    def test_flights_deployment_says_nothing_about_hotels(self):
        text = self._instructions("flights")
        assert "hotel" not in text.lower()
        assert "search_oneway_flights" in text

    def test_flights_states_the_real_fan_out_cap(self):
        """Same defect class as the free server's upgrade note: a cap that is
        real must not be described as absent."""
        text = self._instructions("flights")
        assert str(HARD_MAX_SEARCHES) in text
        assert "max_searches" in text

    def test_both_deployment_covers_both_products(self):
        text = self._instructions("both")
        assert "search_oneway_flights" in text
        assert "search_hotels" in text

    @pytest.mark.parametrize("products", ("flights", "hotels", "both"))
    def test_every_deployment_warns_that_prices_go_stale(self, products):
        text = self._instructions(products)
        assert "stale" in text

    @pytest.mark.parametrize("products", ("flights", "hotels", "both"))
    def test_every_deployment_explains_how_to_pass_a_key(self, products):
        text = self._instructions(products)
        assert "x-rapidapi-key" in text
        assert self.SIGNUP[products] in text

    @pytest.mark.asyncio
    async def test_the_string_reaches_the_client_over_the_wire(self):
        """Asserting the builder is not enough -- the bug was that the value
        never came from a builder at all."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products="hotels",
            signup_url=self.SIGNUP["hotels"],
        )
        async with Client(mcp) as client:
            text = client.initialize_result.instructions or ""
        assert "hotel" in text.lower()
        assert "flight" not in self._without_the_brand(text).lower()
