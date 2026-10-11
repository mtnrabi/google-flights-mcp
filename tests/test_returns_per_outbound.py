"""`returns_per_outbound` on search_roundtrip_flights (backend flight_rabbi #525).

Optional and omitted from the body when not given, so the default path sends
exactly the body it sent before the argument existed. PRE_CHANGE_BODY below
was recorded from origin/main at 3b41ba7 (before this change) with the same
call, through the same MockTransport, so the identity check compares against
the old code path rather than against itself.
"""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from tests.test_server import ONEWAY_ROW, build_with_upstream, call

ARGS = dict(
    from_airport="LHR",
    to_airport="DXB",
    departure_date="2026-12-15",
    return_date="2026-12-22",
    passengers=[2, 1, 0],
    max_return_stops=1,
    seat_type=3,
    currency="eur",
)

PRE_CHANGE_BODY = {
    "_fp_source": "paid-mcp",
    "_fp_tool": "search_roundtrip_flights",
    "currency": "eur",
    "departure_date": "2026-12-15",
    "from_airport": "LHR",
    "limit": 10,
    "max_return_stops": 1,
    "passengers": [1, 1, 2],
    "return_date": "2026-12-22",
    "seat_type": 3,
    "to_airport": "DXB",
}


def _recording_upstream():
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[ONEWAY_ROW])

    return bodies, build_with_upstream(handler)


@pytest.mark.asyncio
async def test_default_body_is_unchanged_and_the_argument_only_adds_its_key():
    without, mcp = _recording_upstream()
    await call(mcp, "search_roundtrip_flights", **ARGS)
    with_arg, mcp = _recording_upstream()
    await call(mcp, "search_roundtrip_flights", returns_per_outbound=5, **ARGS)

    assert without == [PRE_CHANGE_BODY]
    assert with_arg == [{**PRE_CHANGE_BODY, "returns_per_outbound": 5}]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 2, 5])
async def test_in_range_values_are_forwarded(value):
    bodies, mcp = _recording_upstream()
    await call(mcp, "search_roundtrip_flights", returns_per_outbound=value, **ARGS)
    assert bodies and bodies[0]["returns_per_outbound"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 6, -1, 50])
async def test_out_of_range_is_refused_before_any_search(value):
    bodies, mcp = _recording_upstream()
    with pytest.raises(ToolError, match="returns_per_outbound must be a whole number from 1 to 5"):
        await call(mcp, "search_roundtrip_flights", returns_per_outbound=value, **ARGS)
    assert bodies == []


@pytest.mark.asyncio
async def test_a_fraction_is_refused_before_any_search():
    bodies, mcp = _recording_upstream()
    with pytest.raises(ToolError, match="returns_per_outbound"):
        await call(mcp, "search_roundtrip_flights", returns_per_outbound=2.5, **ARGS)
    assert bodies == []


def test_schema_documents_it_on_the_round_trip_tool_only():
    from tests.test_directory_readiness import tools_for

    tools = {t["name"]: t for t in tools_for("flights")}
    prop = tools["search_roundtrip_flights"]["inputSchema"]["properties"][
        "returns_per_outbound"
    ]
    assert "1 to 5 (default 2)" in prop["description"]
    assert (
        "Each date/destination combination searched still returns at most 10 "
        "trips; at 5 they are picked from up to 50 pairings instead of 20."
    ) in prop["description"]
    assert "returns_per_outbound" not in tools["search_oneway_flights"]["inputSchema"]["properties"]
    assert "returns_per_outbound" not in tools["search_roundtrip_flights"]["inputSchema"].get(
        "required", []
    )
