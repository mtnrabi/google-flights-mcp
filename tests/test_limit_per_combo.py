"""`limit` must never hide a destination that was searched and answered.

Found on the free server 2026-09-06 and fixed on both, because both merge a
fan-out into one price-sorted list and both used to cut it with a plain
slice. A five-destination, three-date search with `limit: 50` returned no
Lisbon row at all -- Lisbon was searched, answered, and dropped, while
`search_coverage.destinations_searched` still named it. On this server it
costs the caller more than a bad answer: those fifteen searches are fifteen
requests billed to their own RapidAPI plan, and three of the destinations
they paid for were not in the response.

See mcp_server/tests/test_limit_per_combo.py for the same scenario against
the free server; the selection code is the same on both.
"""

import json

import httpx
import pytest

from src.server import _select_rows
from tests.test_server import build_with_upstream, call

DESTINATIONS = ["BCN", "LIS", "ATH", "IST", "CDG"]
DATES = ["2026-10-06", "2026-10-07", "2026-10-08"]
BASE_PRICE = {"CDG": 43, "BCN": 70, "ATH": 86, "IST": 105, "LIS": 900}
ROWS_PER_COMBO = 4
TOTAL_COMBOS = len(DESTINATIONS) * len(DATES)


def priced_upstream(request: httpx.Request) -> httpx.Response:
    """Four rows per combination, priced by destination."""
    payload = json.loads(request.content)
    dest = payload["to_airport"]
    day = payload["departure_date"]
    base = BASE_PRICE[dest]
    return httpx.Response(
        200,
        json=[
            {
                "buy_link": f"https://book/{dest}/{day}/{offset}",
                "price": f"${base + offset}",
                "price_as_number": base + offset,
                "total_price_as_number": base + offset,
                "duration_seconds": 10000 + offset,
                "to_airport": dest,
                "departure_date": day,
                "airline": "Test Air",
            }
            for offset in range(ROWS_PER_COMBO)
        ],
    )


def build():
    return build_with_upstream(priced_upstream, max_searches_per_tool_call=TOTAL_COMBOS)


ONEWAY_ARGS = {
    "from_airport": "BER",
    "to_airport": DESTINATIONS,
    "departure_date_from": DATES[0],
    "departure_date_to": DATES[-1],
    "sort_by": "price",
}


class TestTheLisbonCase:
    @pytest.mark.asyncio
    async def test_every_searched_destination_has_a_row(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=20)

        assert data["result_count"] == 20
        shown = {row["to_airport"] for row in data["results"]}
        assert shown == set(DESTINATIONS)
        assert set(data["search_coverage"]["destinations_searched"]) == shown

    @pytest.mark.asyncio
    async def test_the_expensive_destination_gets_one_row_per_date(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=20)

        lisbon = [row for row in data["results"] if row["to_airport"] == "LIS"]
        assert {row["departure_date"] for row in lisbon} == set(DATES)

    @pytest.mark.asyncio
    async def test_results_are_still_sorted_by_price(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=20)

        prices = [row["price_as_number"] for row in data["results"]]
        assert prices == sorted(prices)
        assert prices[0] == BASE_PRICE["CDG"]

    @pytest.mark.asyncio
    async def test_the_rest_of_limit_still_goes_to_the_cheapest(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=20)

        counts: dict[str, int] = {}
        for row in data["results"]:
            counts[row["to_airport"]] = counts.get(row["to_airport"], 0) + 1
        assert counts["CDG"] == 8
        assert counts["LIS"] == 3

    @pytest.mark.asyncio
    async def test_roundtrip_has_the_same_guarantee(self):
        data = await call(
            build(),
            "search_roundtrip_flights",
            from_airport="BER",
            to_airport=DESTINATIONS,
            departure_date_from=DATES[0],
            departure_date_to=DATES[-1],
            nights=3,
            sort_by="price",
            limit=20,
        )

        assert {row["to_airport"] for row in data["results"]} == set(DESTINATIONS)


class TestLimitSmallerThanTheFanout:
    @pytest.mark.asyncio
    async def test_coverage_is_marked_truncated_and_names_the_missing(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        coverage = data["search_coverage"]
        assert data["result_count"] == 4
        assert coverage["truncated"] is True
        note = coverage["note"]
        assert "`limit` was 4" in note
        assert "11 of them have no row" in note
        assert "2026-10-06 to LIS" in note
        assert "and 3 more" in note

    @pytest.mark.asyncio
    async def test_a_generous_limit_says_nothing_about_hidden_rows(self):
        data = await call(
            build(), "search_oneway_flights", **ONEWAY_ARGS, limit=TOTAL_COMBOS * ROWS_PER_COMBO
        )

        assert data["search_coverage"]["truncated"] is False
        assert "note" not in data["search_coverage"]


class TestSelectRows:
    @staticmethod
    def _group(dest: str, *prices: int):
        return (
            {"departure_date": "2026-10-06", "to_airport": dest},
            [
                {
                    "buy_link": f"https://book/{dest}/{price}",
                    "price_as_number": price,
                    "to_airport": dest,
                }
                for price in prices
            ],
        )

    def test_no_groups_is_no_rows(self):
        assert _select_rows([], "price", 10) == ([], [])

    def test_a_combination_that_found_nothing_is_not_reported_hidden(self):
        groups = [
            self._group("CDG", 40, 50),
            ({"departure_date": "2026-10-06", "to_airport": "LIS"}, []),
        ]
        rows, hidden = _select_rows(groups, "price", 1)
        assert [row["price_as_number"] for row in rows] == [40]
        assert hidden == []

    def test_a_fare_returned_by_two_combinations_counts_once(self):
        shared = {"buy_link": "https://book/same", "price_as_number": 40}
        groups = [
            ({"departure_date": "2026-10-06", "to_airport": "CDG"}, [shared]),
            ({"departure_date": "2026-10-07", "to_airport": "CDG"}, [dict(shared)]),
        ]
        rows, hidden = _select_rows(groups, "price", 10)
        assert len(rows) == 1
        assert hidden == []

    def test_zero_limit_returns_nothing_and_claims_nothing(self):
        assert _select_rows([self._group("CDG", 40)], "price", 0) == ([], [])

    def test_roundtrip_totals_are_the_price_key_too(self):
        groups = [
            (
                {"departure_date": "2026-10-06", "to_airport": "CDG"},
                [{"buy_link": "a", "total_price_as_number": 300}],
            ),
            (
                {"departure_date": "2026-10-06", "to_airport": "LIS"},
                [{"buy_link": "b", "total_price_as_number": 200}],
            ),
        ]
        rows, _hidden = _select_rows(groups, "price", 2)
        assert [row["buy_link"] for row in rows] == ["b", "a"]
