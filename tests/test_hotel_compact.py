"""Compact hotel rows (2026-10-22, Anthropic directory policy 5.B).

The fixture is a real `search_hotels` answer, captured keyed through
hotels.flightpowers.com/mcp at 17:27Z (Lisbon, 2027-03-05..08, 2 adults): 25
properties, 26,552 bytes of structuredContent, more than half of it Booking
links and image URLs. The size tests run the same rows through the server and
pin what a model is handed by default.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.hotel_compact import (
    HOTEL_DEFAULT_LIMIT,
    compact_hotel_row,
    select_rows,
    short_link,
)
from tests.test_hotel_date_range import recording_handler, room
from tests.test_server import build_with_upstream

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

LIVE = json.loads(
    (
        pathlib.Path(__file__).parent / "data" / "hotels_search_lisbon_live_2026-10-22.json"
    ).read_text()
)
STAY = {
    "destination": "Lisbon",
    "checkin_date": "2027-03-05",
    "checkout_date": "2027-03-08",
    "adults": 2,
    "currency": "usd",
}


def live_handler(_request):
    return httpx.Response(
        200, json=LIVE, headers={"x-ratelimit-requests-remaining": "19990"}
    )


async def call_whole(mcp, tool: str, **kwargs):
    """structuredContent and the total size of the result a host carries."""
    async with Client(mcp) as client:
        out = await client.call_tool(tool, kwargs)
    text = sum(len(c.text.encode()) for c in out.content if hasattr(c, "text"))
    structured = len(json.dumps(out.structured_content, ensure_ascii=False).encode())
    return out.structured_content, structured, text


class TestShortLink:
    LONG = LIVE["properties"][0]["link"]

    def test_keeps_the_stay_and_drops_the_tracking(self):
        got = short_link(self.LONG)
        assert got == (
            "https://www.booking.com/hotel/pt/zenitlisboa.html?checkin=2027-03-05"
            "&checkout=2027-03-08&group_adults=2&no_rooms=1&group_children=0"
            "&highlighted_blocks=3872305_95163064_0_42_0_456042"
        )
        for gone in ("aid=", "label=", "srpvid=", "srepoch=", "sr_pri_blocks="):
            assert gone not in got

    def test_child_ages_survive(self):
        url = (
            "https://www.booking.com/hotel/it/x.html?aid=1&checkin=2027-01-01"
            "&checkout=2027-01-03&group_adults=2&group_children=2&age=4&age=9"
        )
        assert short_link(url).endswith("group_children=2&age=4&age=9")

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/hotel?aid=1&label=x",
            "https://notbooking.com/x?label=1",
            "https://www.booking.com/hotel/it/x.html",
            "not a url",
            None,
        ],
    )
    def test_anything_else_is_left_alone(self, url):
        assert short_link(url) == url


class TestRowShape:
    def test_drops_images_only_and_adds_per_night(self):
        row = LIVE["properties"][0]
        got = compact_hotel_row(row)
        assert "image_url" not in got
        assert got["price_per_night"] == 95.0  # 285 for 3 nights
        for kept in (
            "name", "price_string", "price", "review_score", "review_count",
            "room_type", "location", "nights", "latitude", "longitude",
            "retrieved_at", "link",
        ):
            assert kept in got
        assert set(got) == (set(row) - {"image_url"}) | {"price_per_night"}

    def test_no_per_night_without_a_price_or_nights(self):
        assert "price_per_night" not in compact_hotel_row(room("A", None, nights=3))
        assert "price_per_night" not in compact_hotel_row(room("A", 300.0))


class TestSelectRows:
    def test_head_in_source_order_when_the_cheapest_is_in_it(self):
        rows = [room("A", 300.0), room("B", 100.0), room("C", 50.0)]
        assert select_rows(rows, 3) == rows
        rows = [room("A", 30.0), room("B", 100.0), room("C", 50.0)]
        assert [r["name"] for r in select_rows(rows, 2)] == ["A", "B"]

    def test_the_cheapest_takes_the_last_slot_when_cut(self):
        rows = [room("A", 300.0), room("B", 200.0), room("C", None), room("D", 50.0)]
        assert [r["name"] for r in select_rows(rows, 2)] == ["A", "D"]
        assert [r["name"] for r in select_rows(rows, 1)] == ["D"]

    def test_live_lisbon_keeps_the_cheapest(self):
        # The cheapest (US$188) is 10th of 25 in Booking's order.
        kept = select_rows(LIVE["properties"], 5)
        assert min(r["price"] for r in kept) == 188


class TestThroughTheServer:
    @pytest.mark.asyncio
    async def test_default_answer_is_bounded_and_compact(self):
        mcp = build_with_upstream(live_handler, fallback_rapidapi_key=KEY)
        out, structured, text = await call_whole(mcp, "search_hotels", **STAY)
        assert out["result_count"] == HOTEL_DEFAULT_LIMIT
        assert out["results_total"] == 25
        assert "A `limit` up to 25 returns more." in out["results_note"]
        assert [r["name"] for r in out["results"]] == [
            r["name"] for r in LIVE["properties"][:HOTEL_DEFAULT_LIMIT]
        ]
        assert min(r["price"] for r in out["results"]) == 188
        for row in out["results"]:
            assert "image_url" not in row
            assert "label=" not in row["link"]
            assert row["price_per_night"] > 0

    @pytest.mark.asyncio
    async def test_size_before_and_after(self):
        mcp = build_with_upstream(live_handler, fallback_rapidapi_key=KEY)
        _, full_s, full_t = await call_whole(mcp, "search_hotels", **STAY, verbose=True)
        _, all_s, _ = await call_whole(mcp, "search_hotels", **STAY, limit=25)
        _, def_s, def_t = await call_whole(mcp, "search_hotels", **STAY)
        # The old answer: every row, every field (~26 KB, mirrored as text).
        assert full_s > 26_000
        # All 25 rows compact: the row shape alone takes off ~45%.
        assert all_s < full_s * 0.6
        # The default: under a quarter of the old answer, text mirror included
        # (measured in-process: 52,139 -> 11,905 bytes).
        assert def_s + def_t < (full_s + full_t) * 0.25
        assert def_s < 7_000

    @pytest.mark.asyncio
    async def test_verbose_is_the_old_answer(self):
        mcp = build_with_upstream(live_handler, fallback_rapidapi_key=KEY)
        out, _, _ = await call_whole(mcp, "search_hotels", **STAY, verbose=True)
        assert out["results"] == LIVE["properties"]
        assert out["result_count"] == 25
        assert "results_total" not in out and "results_note" not in out

    @pytest.mark.asyncio
    async def test_verbose_with_a_limit_bounds_rows_but_not_fields(self):
        mcp = build_with_upstream(live_handler, fallback_rapidapi_key=KEY)
        out, _, _ = await call_whole(
            mcp, "search_hotels", **STAY, verbose=True, limit=3
        )
        assert out["result_count"] == 3
        assert all("image_url" in r for r in out["results"])
        assert out["results"][-1]["price"] == 188

    @pytest.mark.asyncio
    async def test_limit_below_one_is_refused_before_any_search(self):
        seen = []

        def handler(request):
            seen.append(request)
            return live_handler(request)

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="limit"):
                await client.call_tool("search_hotels", {**STAY, "limit": 0})
        assert seen == []

    @pytest.mark.asyncio
    async def test_result_rows_max_zero_restores_the_old_answer(self):
        mcp = build_with_upstream(
            live_handler, fallback_rapidapi_key=KEY, result_rows_max=0
        )
        out, _, _ = await call_whole(mcp, "search_hotels", **STAY)
        assert out["results"] == LIVE["properties"]
        assert "results_total" not in out

    @pytest.mark.asyncio
    async def test_by_name_row_is_compact_and_verbose_is_untouched(self):
        row = {**LIVE["properties"][0], "matched_name": "Zenit Lisboa", "match_confidence": 0.9}

        def handler(_r):
            return httpx.Response(200, json=row)

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        args = {
            "hotel_name": "Zenit Lisboa",
            "checkin_date": "2027-03-05",
            "checkout_date": "2027-03-08",
        }
        out, _, _ = await call_whole(mcp, "find_hotel_by_name", **args)
        assert out["results"] == [compact_hotel_row(row)]
        assert out["results"][0]["match_confidence"] == 0.9
        out, _, _ = await call_whole(mcp, "find_hotel_by_name", **args, verbose=True)
        assert out["results"] == [row]

    @pytest.mark.asyncio
    async def test_by_name_never_drops_a_row_or_points_at_a_missing_limit(self):
        # find_hotel_by_name has no `limit`, so it must never cut rows and
        # never tell a model to pass one.
        def handler(_r):
            return httpx.Response(200, json=LIVE["properties"][:12])

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out, _, _ = await call_whole(
            mcp,
            "find_hotel_by_name",
            hotel_name="Zenit Lisboa",
            checkin_date="2027-03-05",
            checkout_date="2027-03-08",
        )
        assert out["result_count"] == 12
        assert "results_note" not in out
        assert all("image_url" not in r for r in out["results"])

    @pytest.mark.asyncio
    async def test_limit_is_declared_with_its_floor(self):
        mcp = build_with_upstream(live_handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        prop = tools["search_hotels"].inputSchema["properties"]["limit"]
        assert {"minimum": 1, "type": "integer"} in prop["anyOf"]
        assert "cheapest is always" in prop["description"]
        assert "limit" not in tools["find_hotel_by_name"].inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_a_range_compacts_the_winner_rows_and_the_summaries(self):
        rows = LIVE["properties"]

        def price_for(ci, _co):
            return rows if ci == "2027-03-06" else rows[:3]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out, structured, _ = await call_whole(
            mcp,
            "search_hotels",
            destination="Lisbon",
            checkin_date_from="2027-03-05",
            checkin_date_to="2027-03-07",
            nights=3,
        )
        assert out["result_count"] == HOTEL_DEFAULT_LIMIT
        assert out["results_total"] == 25
        assert "for this stay" in out["results_note"]
        for stay in out["stays"]:
            if stay["cheapest"]:
                assert "label=" not in stay["cheapest"]["link"]
        assert "label=" not in out["cheapest_overall"]["property"]["link"]
        assert structured < 12_000
