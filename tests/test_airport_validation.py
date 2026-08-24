"""
Airport codes are checked before the request, not after.

The upstream answers a blank or malformed code with `200 []` rather than an
error, so without this guard a model that passes an empty string is told
"no flights found" -- indistinguishable from a genuinely empty route, and
invisible in every log. The same reasoning already guards hotel filters; this
applies it to the one field a model is most likely to leave empty.

Found while preparing the ChatGPT directory submission, which requires
demonstrable negative cases: there was no way to show a refusal because the
server did not refuse.
"""

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.rapidapi_client import invalid_airports
from tests.test_server import build_with_upstream

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class TestInvalidAirports:
    def test_good_codes_pass(self):
        assert invalid_airports("TLV", "LCA") == []

    def test_lowercase_is_fine(self):
        # The payload builder upper-cases; rejecting here would be surprising.
        assert invalid_airports("tlv", "lca") == []

    def test_empty_string_is_named_not_blank(self):
        """An error listing '' tells the caller nothing they can see."""
        assert invalid_airports("") == ["(empty)"]

    def test_whitespace_only_is_empty(self):
        assert invalid_airports("   ") == ["(empty)"]

    def test_wrong_length_is_rejected(self):
        assert invalid_airports("T") == ["T"]
        assert invalid_airports("TLVX") == ["TLVX"]

    def test_digits_are_rejected(self):
        # "Tel Aviv" or "TL5" are the shapes a model invents when guessing.
        assert invalid_airports("TL5") == ["TL5"]
        assert invalid_airports("Tel Aviv") == ["Tel Aviv"]

    def test_none_is_skipped_not_flagged(self):
        assert invalid_airports(None) == []

    def test_a_list_of_destinations_is_checked_elementwise(self):
        assert invalid_airports("TLV", ["LCA", "", "ATHX"]) == ["(empty)", "ATHX"]


class TestToolsRejectBeforeSpending:
    @pytest.mark.asyncio
    async def test_empty_origin_costs_nothing(self):
        """The exact call the n8n agent made: no arguments, empty strings."""
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "",
                        "to_airport": "",
                        "departure_date": "2026-10-15",
                    },
                )
        assert "airport codes" in str(exc.value)
        assert calls == [], "an unusable code must not cost a billed request"

    @pytest.mark.asyncio
    async def test_roundtrip_is_guarded_too(self):
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "search_roundtrip_flights",
                    {
                        "from_airport": "Tel Aviv",
                        "to_airport": "LCA",
                        "departure_date": "2026-10-15",
                        "return_date": "2026-10-22",
                    },
                )
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_message_names_the_bad_code(self):
        """A model needs to know which argument to fix, not just that one is
        wrong -- otherwise it retries the same call."""

        def handler(_r):
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "NOPE1",
                        "departure_date": "2026-10-15",
                    },
                )
        # Only the offending code is listed. Asserting "TLV" is absent from the
        # whole message would be wrong -- it appears as the example of a good
        # code -- so pin the listed set instead.
        assert "Not valid airport codes: NOPE1." in str(exc.value)

    @pytest.mark.asyncio
    async def test_valid_codes_still_search(self):
        """The guard must not break the happy path."""
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(200, json=[])

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            out = await client.call_tool(
                "search_oneway_flights",
                {
                    "from_airport": "tlv",
                    "to_airport": "LCA",
                    "departure_date": "2026-10-15",
                },
            )
        assert out.data["result_count"] == 0
        assert len(calls) == 1
