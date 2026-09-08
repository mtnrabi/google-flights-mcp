"""Every destination the caller asked for has an entry, empty or not.

The same shape as the free server (see mcp_server/tests/test_by_destination.py
for the reasoning), and it matters more here: each date/destination
combination is one request billed to the caller's own RapidAPI plan. A
destination that was searched, paid for and then left out of `results` is a
charge with nothing to show for it, and a destination the fan-out cap never
searched at all is not a charge -- telling those two apart is the difference
between "there are no flights to Lisbon" and "you have not asked about
Lisbon yet".

    ok            -- it has rows in `results`
    no_flights    -- searched, answered, Google has nothing
    search_failed -- searched and the search errored; nothing is known
    not_in_limit  -- searched, found flights, none fitted in `limit`
    not_searched  -- never searched; the fan-out cap sampled it away
"""

import json

import httpx
import pytest

from src.fanout import SearchPlan
from src.server import _by_destination
from tests.test_server import build_with_upstream, call

DESTINATIONS = ["BCN", "LIS", "ATH", "IST", "CDG"]
DATES = ["2026-10-06", "2026-10-07", "2026-10-08"]
BASE_PRICE = {"CDG": 43, "BCN": 70, "ATH": 86, "IST": 105, "LIS": 900}
ROWS_PER_COMBO = 4
TOTAL_COMBOS = len(DESTINATIONS) * len(DATES)

ONEWAY_ARGS = {
    "from_airport": "BER",
    "to_airport": DESTINATIONS,
    "departure_date_from": DATES[0],
    "departure_date_to": DATES[-1],
    "sort_by": "price",
}


def upstream(*, empty=frozenset(), failing=frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        dest = payload["to_airport"]
        day = payload["departure_date"]
        if dest in failing:
            return httpx.Response(500, text="upstream refused")
        if dest in empty:
            return httpx.Response(200, json=[])
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

    return handler


def build(*, empty=frozenset(), failing=frozenset(), cap=TOTAL_COMBOS):
    return build_with_upstream(
        upstream(empty=empty, failing=failing), max_searches_per_tool_call=cap
    )


class TestTheFixedShape:
    @pytest.mark.asyncio
    async def test_every_requested_destination_has_an_entry(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        assert list(data["by_destination"]) == DESTINATIONS

    @pytest.mark.asyncio
    async def test_the_expensive_destination_is_not_an_empty_entry(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        lisbon = data["by_destination"]["LIS"]
        assert lisbon["searched"] is True
        assert lisbon["reason"] == "ok"
        assert lisbon["cheapest"]["price_as_number"] == BASE_PRICE["LIS"]

    @pytest.mark.asyncio
    async def test_entry_rows_are_the_rows_in_results(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=4)

        regrouped = [
            row for entry in data["by_destination"].values() for row in entry["rows"]
        ]
        assert len(regrouped) == data["result_count"]
        assert {row["buy_link"] for row in regrouped} == {
            row["buy_link"] for row in data["results"]
        }

    @pytest.mark.asyncio
    async def test_cheapest_is_the_cheapest_of_that_destination(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=60)

        for dest, entry in data["by_destination"].items():
            prices = [row["price_as_number"] for row in entry["rows"]]
            assert entry["cheapest"]["price_as_number"] == min(prices) == BASE_PRICE[dest]

    @pytest.mark.asyncio
    async def test_roundtrip_answers_in_the_same_shape(self):
        data = await call(
            build(),
            "search_roundtrip_flights",
            from_airport="BER",
            to_airport=DESTINATIONS,
            departure_date_from=DATES[0],
            departure_date_to=DATES[-1],
            nights=3,
            sort_by="price",
            limit=4,
        )

        assert list(data["by_destination"]) == DESTINATIONS


class TestTheHoles:
    @pytest.mark.asyncio
    async def test_a_destination_with_no_flights_is_present_and_says_so(self):
        data = await call(
            build(empty={"LIS"}), "search_oneway_flights", **ONEWAY_ARGS, limit=20
        )

        lisbon = data["by_destination"]["LIS"]
        assert lisbon["rows"] == []
        assert lisbon["cheapest"] is None
        assert lisbon["searched"] is True
        assert lisbon["reason"] == "no_flights"

    @pytest.mark.asyncio
    async def test_a_destination_whose_searches_failed_says_so(self):
        data = await call(
            build(failing={"ATH"}), "search_oneway_flights", **ONEWAY_ARGS, limit=20
        )

        athens = data["by_destination"]["ATH"]
        assert athens["rows"] == []
        assert athens["searched"] is True
        assert athens["reason"] == "search_failed"

    @pytest.mark.asyncio
    async def test_an_empty_and_a_failed_destination_are_told_apart(self):
        data = await call(
            build(empty={"LIS"}, failing={"ATH"}),
            "search_oneway_flights",
            **ONEWAY_ARGS,
            limit=20,
        )

        assert data["by_destination"]["LIS"]["reason"] == "no_flights"
        assert data["by_destination"]["ATH"]["reason"] == "search_failed"

    @pytest.mark.asyncio
    async def test_a_destination_the_fanout_cap_dropped_is_not_searched(self):
        """`max_searches` below the fan-out is the paid server's own knob, so
        this one is provoked end to end rather than constructed."""
        data = await call(
            build(cap=4), "search_oneway_flights", **ONEWAY_ARGS, limit=20
        )

        by_dest = data["by_destination"]
        assert list(by_dest) == DESTINATIONS, "all five, though four searches ran"
        never = [d for d, entry in by_dest.items() if entry["reason"] == "not_searched"]
        assert never, "the cap dropped whole destinations and the shape shows it"
        for dest in never:
            assert by_dest[dest]["searched"] is False
            assert by_dest[dest]["rows"] == []
        assert data["search_coverage"]["truncated"] is True


class TestPerDate:
    @pytest.mark.asyncio
    async def test_a_multi_date_search_breaks_each_destination_down_by_date(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=20)

        for entry in data["by_destination"].values():
            assert list(entry["dates"]) == DATES

    @pytest.mark.asyncio
    async def test_a_date_entry_carries_its_own_cheapest_price(self):
        data = await call(build(), "search_oneway_flights", **ONEWAY_ARGS, limit=60)

        for dest, entry in data["by_destination"].items():
            for day_entry in entry["dates"].values():
                assert day_entry["searched"] is True
                assert day_entry["reason"] == "ok"
                assert day_entry["row_count"] == ROWS_PER_COMBO
                assert day_entry["cheapest_price"] == BASE_PRICE[dest]

    @pytest.mark.asyncio
    async def test_a_date_the_cap_dropped_shows_as_not_searched(self):
        data = await call(
            build(cap=4), "search_oneway_flights", **ONEWAY_ARGS, limit=20
        )

        missed = [
            (dest, day)
            for dest, entry in data["by_destination"].items()
            for day, day_entry in entry["dates"].items()
            if day_entry["reason"] == "not_searched"
        ]
        assert len(missed) == TOTAL_COMBOS - 4

    @pytest.mark.asyncio
    async def test_a_single_date_search_has_no_date_breakdown(self):
        data = await call(
            build(),
            "search_oneway_flights",
            from_airport="BER",
            to_airport=DESTINATIONS,
            departure_date=DATES[0],
            sort_by="price",
            limit=20,
        )

        for entry in data["by_destination"].values():
            assert "dates" not in entry


class TestByDestinationDirectly:
    @staticmethod
    def _plan(executed: list[tuple[str, str]]) -> SearchPlan:
        requested = [
            {"departure_date": day, "to_airport": dest}
            for day in DATES[:2]
            for dest in ["CDG", "LIS"]
        ]
        return SearchPlan(
            endpoint="oneway",
            combos=[
                {"departure_date": day, "to_airport": dest} for dest, day in executed
            ],
            requested_combinations=len(requested),
            cap=len(executed),
            requested_combos=requested,
        )

    def test_a_destination_that_found_flights_and_missed_the_limit(self):
        plan = self._plan([("CDG", DATES[0]), ("LIS", DATES[0])])
        cheap = {"buy_link": "a", "price_as_number": 40}
        dear = {"buy_link": "b", "price_as_number": 900}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [cheap]),
            ({"departure_date": DATES[0], "to_airport": "LIS"}, [dear]),
        ]

        by_dest = _by_destination(plan, groups, [], [cheap], [0])

        assert by_dest["LIS"]["reason"] == "not_in_limit"
        assert by_dest["LIS"]["searched"] is True

    def test_rows_stay_in_results_order_within_a_destination(self):
        plan = self._plan([("CDG", DATES[0]), ("CDG", DATES[1])])
        first = {"buy_link": "a", "price_as_number": 40}
        second = {"buy_link": "b", "price_as_number": 50}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [second]),
            ({"departure_date": DATES[1], "to_airport": "CDG"}, [first]),
        ]

        by_dest = _by_destination(plan, groups, [], [first, second], [1, 0])

        assert [row["buy_link"] for row in by_dest["CDG"]["rows"]] == ["a", "b"]

    def test_a_roundtrip_pair_the_planner_skipped_is_not_claimed_as_a_hole(self):
        """A return date before a departure date is never planned, so there is
        no hole to report -- inventing one would be as wrong as hiding a real
        one."""
        plan = SearchPlan(
            endpoint="roundtrip",
            combos=[
                {
                    "departure_date": DATES[0],
                    "return_date": DATES[1],
                    "to_airport": "CDG",
                }
            ],
            requested_combinations=1,
            cap=5,
            requested_combos=[
                {
                    "departure_date": DATES[0],
                    "return_date": DATES[1],
                    "to_airport": "CDG",
                }
            ],
        )
        row = {"buy_link": "a", "total_price_as_number": 300}
        groups = [
            (
                {
                    "departure_date": DATES[0],
                    "return_date": DATES[1],
                    "to_airport": "CDG",
                },
                [row],
            )
        ]

        by_dest = _by_destination(plan, groups, [], [row], [0])

        assert list(by_dest) == ["CDG"]
        assert "dates" not in by_dest["CDG"], "one departure date, no breakdown"
        assert by_dest["CDG"]["cheapest"]["total_price_as_number"] == 300
