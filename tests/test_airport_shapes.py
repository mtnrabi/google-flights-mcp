"""One search, three ways of writing the destination list.

Verified live on 2026-09-06, against main after #444:

    free  search_oneway_flights(to_airport="BCN,LIS,ATH")  -> fanned out, 3 searches
    paid  search_oneway_flights(to_airport="BCN,LIS,ATH")  -> ToolError,
        "Not valid airport codes: BCN,LIS,ATH. Use three-letter IATA codes"

Same tool name, same argument name, same intent, opposite outcome. An LLM that
had used one of the two servers -- or read one of the two listings -- got a
hard refusal on the other and no hint that the fix was to send a JSON list.
The refusal is also the most expensive kind: it names the string the model
just wrote as the thing that is wrong, so a retry writes it again.

Both servers now take a code, a separated string, or a list, on both flight
tools, for the origin as well as the destination. The invalid-code guard is
unchanged for codes that are genuinely bad -- see test_airport_validation.py.
The mirror of this file is mcp_server/tests/test_airport_shapes.py.
"""

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.fanout import (
    PlanError,
    normalise_airport_codes,
    normalise_destinations,
    normalise_origin,
    plan_oneway,
    plan_roundtrip,
    split_airport_codes,
)
from src.rapidapi_client import invalid_airports
from tests.test_server import build_with_upstream

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class TestSplitAirportCodes:
    """The one helper both servers normalise through."""

    def test_a_single_code(self):
        assert split_airport_codes("BCN") == ["BCN"]

    def test_a_comma_separated_string(self):
        assert split_airport_codes("BCN,LIS,ATH") == ["BCN", "LIS", "ATH"]

    def test_commas_with_spaces(self):
        assert split_airport_codes("BCN, LIS,  ATH") == ["BCN", "LIS", "ATH"]

    def test_spaces_alone(self):
        assert split_airport_codes("BCN LIS ATH") == ["BCN", "LIS", "ATH"]

    def test_semicolons_and_pipes(self):
        assert split_airport_codes("BCN;LIS|ATH") == ["BCN", "LIS", "ATH"]

    def test_a_json_list(self):
        assert split_airport_codes(["BCN", "LIS"]) == ["BCN", "LIS"]

    def test_a_list_whose_elements_are_themselves_strings_of_codes(self):
        """Seen from hosts that join a user's phrase and then wrap it."""
        assert split_airport_codes(["BCN,LIS", "ATH"]) == ["BCN", "LIS", "ATH"]

    def test_case_is_preserved_here(self):
        # Upper-casing belongs to `normalise_airport_codes`; keeping the
        # caller's text here is what lets an error name what they wrote.
        assert split_airport_codes("bcn,Lis") == ["bcn", "Lis"]

    def test_nothing_is_nothing(self):
        assert split_airport_codes(None) == []
        assert split_airport_codes("") == []
        assert split_airport_codes("   ") == []
        assert split_airport_codes([]) == []

    def test_a_phrase_is_not_split_on_its_spaces(self):
        """"Tel Aviv" is one bad value, not the two bad values TEL and AVIV.

        Whitespace separates only when every piece already looks like a code,
        so the error names the string the model actually wrote.
        """
        assert split_airport_codes("Tel Aviv") == ["Tel Aviv"]
        assert split_airport_codes("Tel Aviv (TLV)") == ["Tel Aviv (TLV)"]
        assert split_airport_codes("New York,Tel Aviv") == ["New York", "Tel Aviv"]


class TestNormaliseAirportCodes:
    def test_upper_cased(self):
        assert normalise_airport_codes("bcn,lis") == ["BCN", "LIS"]

    def test_mixed_case_and_padding(self):
        assert normalise_airport_codes(" bcn , LiS ") == ["BCN", "LIS"]

    def test_duplicates_drop_and_order_is_the_callers(self):
        assert normalise_airport_codes("LIS,bcn,LIS,ATH") == ["LIS", "BCN", "ATH"]

    def test_duplicates_across_shapes(self):
        assert normalise_airport_codes(["bcn", "BCN,ath"]) == ["BCN", "ATH"]


class TestOriginShapes:
    def test_a_plain_code(self):
        assert normalise_origin("tlv") == "TLV"

    def test_padded(self):
        assert normalise_origin("  TLV ") == "TLV"

    def test_a_one_element_list(self):
        assert normalise_origin(["tlv"]) == "TLV"

    def test_the_same_code_twice_is_still_one_origin(self):
        assert normalise_origin("TLV,tlv") == "TLV"

    def test_two_origins_are_refused_by_name(self):
        """Before this the string was upper-cased and sent whole, and the
        upstream answered `200 []` -- which reads as "no flights"."""
        with pytest.raises(PlanError) as exc:
            normalise_origin("TLV,JFK")
        assert "one origin airport per search" in str(exc.value)
        assert "TLV, JFK" in str(exc.value)

    def test_a_missing_origin_is_named(self):
        with pytest.raises(PlanError):
            normalise_origin("")


class TestDestinationShapes:
    def test_empty_still_raises(self):
        with pytest.raises(PlanError):
            normalise_destinations("")
        with pytest.raises(PlanError):
            normalise_destinations([])

    def test_the_planner_takes_every_shape(self):
        as_string = plan_oneway(
            from_airport="TLV",
            to_airport="BCN,LIS,ATH",
            departure_date="2026-10-14",
            cap=15,
        )
        as_spaces = plan_oneway(
            from_airport="TLV",
            to_airport="bcn lis ath",
            departure_date="2026-10-14",
            cap=15,
        )
        as_list = plan_oneway(
            from_airport="TLV",
            to_airport=["BCN", "LIS", "ATH"],
            departure_date="2026-10-14",
            cap=15,
        )
        assert as_string.combos == as_list.combos == as_spaces.combos
        assert [c["to_airport"] for c in as_list.combos] == ["BCN", "LIS", "ATH"]

    def test_the_roundtrip_planner_too(self):
        plan = plan_roundtrip(
            from_airport="tlv",
            to_airport="bcn, lis",
            departure_date="2026-10-14",
            nights=5,
            cap=15,
        )
        assert [c["to_airport"] for c in plan.combos] == ["BCN", "LIS"]

    def test_a_multi_origin_plan_is_refused(self):
        with pytest.raises(PlanError):
            plan_oneway(
                from_airport="TLV,JFK",
                to_airport="BCN",
                departure_date="2026-10-14",
                cap=15,
            )


class TestInvalidAirportsAcceptsTheSameShapes:
    """The guard that used to reject a comma string as one 11-letter code."""

    def test_a_comma_string_of_good_codes_is_fine(self):
        assert invalid_airports("TLV", "BCN,LIS,ATH") == []

    def test_spaces_and_semicolons_too(self):
        assert invalid_airports("TLV", "BCN LIS ATH") == []
        assert invalid_airports("TLV", "bcn;lis") == []

    def test_only_the_bad_member_of_a_string_is_named(self):
        assert invalid_airports("TLV", "BCN,NOPE1,ATH") == ["NOPE1"]

    def test_a_phrase_is_reported_as_the_caller_wrote_it(self):
        assert invalid_airports("Tel Aviv") == ["Tel Aviv"]

    def test_a_multi_code_origin_is_not_a_code_error(self):
        """It is a different mistake with a different fix, and the planner
        gives it a message that says so."""
        assert invalid_airports("TLV,JFK") == []


def _recording_upstream(seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    return handler


async def _destinations_searched(args: dict) -> list[str]:
    seen: list[dict] = []
    mcp = build_with_upstream(_recording_upstream(seen), fallback_rapidapi_key=KEY)
    async with Client(mcp) as client:
        await client.call_tool("search_oneway_flights", args)
    return [payload["to_airport"] for payload in seen]


BASE = {"from_airport": "TLV", "departure_date": "2026-10-14"}


class TestTheToolsThemselves:
    @pytest.mark.asyncio
    async def test_a_comma_string_fans_out(self):
        """The exact call that raised on 2026-09-06."""
        assert await _destinations_searched(
            {**BASE, "to_airport": "BCN,LIS,ATH"}
        ) == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_a_space_separated_string_fans_out(self):
        assert await _destinations_searched(
            {**BASE, "to_airport": "BCN LIS ATH"}
        ) == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_a_list_still_fans_out(self):
        assert await _destinations_searched(
            {**BASE, "to_airport": ["BCN", "LIS", "ATH"]}
        ) == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_one_code_is_one_search(self):
        assert await _destinations_searched({**BASE, "to_airport": "BCN"}) == ["BCN"]

    @pytest.mark.asyncio
    async def test_mixed_case_reaches_the_backend_upper_cased(self):
        assert await _destinations_searched(
            {**BASE, "to_airport": "bcn, Lis"}
        ) == ["BCN", "LIS"]

    @pytest.mark.asyncio
    async def test_the_origin_is_normalised_the_same_way(self):
        seen: list[dict] = []
        mcp = build_with_upstream(_recording_upstream(seen), fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            await client.call_tool(
                "search_oneway_flights",
                {"from_airport": "  tlv ", "to_airport": "BCN",
                 "departure_date": "2026-10-14"},
            )
        assert seen[0]["from_airport"] == "TLV"

    @pytest.mark.asyncio
    async def test_the_origin_stays_a_single_string_in_the_schema(self):
        """Deliberate: the fan-out is planned over dates and destinations
        only, so advertising `from_airport` as string-or-array would invite a
        call the server has to refuse."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), fallback_rapidapi_key=KEY
        )
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            spec = tools[name].inputSchema["properties"]["from_airport"]
            assert spec.get("type") == "string", spec

    @pytest.mark.asyncio
    async def test_two_origins_are_refused_before_anything_is_billed(self):
        seen: list[dict] = []
        mcp = build_with_upstream(_recording_upstream(seen), fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_oneway_flights",
                    {"from_airport": "TLV,JFK", "to_airport": "BCN",
                     "departure_date": "2026-10-14"},
                )
        assert "one origin airport per search" in str(exc.value)
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_bad_code_inside_a_string_is_still_refused(self):
        seen: list[dict] = []
        mcp = build_with_upstream(_recording_upstream(seen), fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_oneway_flights",
                    {"from_airport": "TLV", "to_airport": "BCN,NOPE1,ATH",
                     "departure_date": "2026-10-14"},
                )
        # Only the offending member, not the whole string the caller wrote.
        assert "Not valid airport codes: NOPE1." in str(exc.value)
        assert seen == [], "a rejected search must not cost a billed request"

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_behaves_the_same(self):
        seen: list[dict] = []
        mcp = build_with_upstream(_recording_upstream(seen), fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            await client.call_tool(
                "search_roundtrip_flights",
                {"from_airport": "tlv", "to_airport": "bcn;lis",
                 "departure_date": "2026-10-14", "nights": 5},
            )
        assert [p["to_airport"] for p in seen] == ["BCN", "LIS"]
        assert {p["from_airport"] for p in seen} == {"TLV"}


class TestTheSchemaSaysSo:
    """A model that cannot see the shapes in the schema keeps guessing."""

    @pytest.mark.asyncio
    async def test_both_tools_document_all_three_shapes(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), fallback_rapidapi_key=KEY
        )
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            described = tools[name].inputSchema["properties"]["to_airport"][
                "description"
            ]
            assert "commas" in described
            assert "list" in described
