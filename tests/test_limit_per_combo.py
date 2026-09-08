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

from src.server import (
    MAX_AUTO_LIMIT,
    _effective_limit,
    _note_hidden_combinations,
    _select_rows,
)
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
    """A `limit` that cannot cover the fan-out is fixed before the search.

    Every combination here is a request billed to the caller's own RapidAPI
    plan, which makes the old behaviour worse on this server than on the free
    one: fifteen paid searches, four rows, and an explanation of which eleven
    they could not see. The number of combinations is known before any of them
    runs, so `limit` is raised to cover them and the coverage says so.
    """

    @pytest.mark.asyncio
    async def test_limit_is_raised_to_cover_every_combination(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        assert data["result_count"] == TOTAL_COMBOS
        assert {row["to_airport"] for row in data["results"]} == set(DESTINATIONS)

    @pytest.mark.asyncio
    async def test_the_note_says_what_was_done_and_why(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        note = data["search_coverage"]["note"]
        assert "`limit` was 4" in note
        assert f"raised to {TOTAL_COMBOS}" in note

    @pytest.mark.asyncio
    async def test_nothing_is_hidden_so_nothing_is_called_truncated(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        assert data["search_coverage"]["truncated"] is False
        assert "have no row" not in data["search_coverage"]["note"]

    @pytest.mark.asyncio
    async def test_a_generous_limit_says_nothing_about_hidden_rows(self):
        data = await call(
            build(), "search_oneway_flights", **ONEWAY_ARGS, limit=TOTAL_COMBOS * ROWS_PER_COMBO
        )

        assert data["search_coverage"]["truncated"] is False
        assert "note" not in data["search_coverage"]

    def test_the_raise_never_lowers_an_explicit_limit(self):
        assert _effective_limit(200, TOTAL_COMBOS) == (200, None)

    def test_the_raise_is_bounded(self):
        raised, note = _effective_limit(10, 500)

        assert raised == MAX_AUTO_LIMIT
        assert f"raised to {MAX_AUTO_LIMIT}" in note

    def test_a_single_combination_needs_no_raise(self):
        assert _effective_limit(1, 1) == (1, None)


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

    def test_a_limit_below_the_answering_combinations_names_them(self):
        """Unreachable through the tool now that the raise happens first, but
        the floor is a general function; this is the last line of defence."""
        groups = [
            self._group("CDG", 40),
            self._group("BCN", 70),
            self._group("LIS", 900),
        ]
        rows, hidden = _select_rows(groups, "price", 2)

        assert [row["to_airport"] for row in rows] == ["CDG", "BCN"]
        assert hidden == ["2026-10-06 to LIS"]

    def test_the_note_for_hidden_combinations_still_reads_correctly(self):
        coverage = {}
        _note_hidden_combinations(coverage, ["2026-10-06 to LIS"], 2)

        assert coverage["truncated"] is True
        assert "`limit` was 2" in coverage["note"]
        assert "2026-10-06 to LIS" in coverage["note"]

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
