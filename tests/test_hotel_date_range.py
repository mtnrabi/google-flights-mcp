"""
Hotels take a check-in range: the flights fan-out, applied to stays.

Why this file exists
--------------------
`POST /search` prices exactly ONE stay. So the question people actually ask --
"what are three nights in Rome in May going to cost me, and which week is
cheapest" -- had two possible answers before this: thirty-one tool calls, or
one call on a date the model picked and an answer presented as the cheapest.
The second is what happens in practice, and it is worse than no answer.

The tests here guard the four things that go wrong silently:

* the expansion itself -- one backend call per stay, the right date pairs;
* the cap -- a wide range must be sampled evenly and SAY it was sampled,
  because a truncated search reads exactly like a complete one;
* the per-stay contract -- a stay whose search errored ("degraded") must never
  be reported as a stay with no rooms ("empty"), and a stay the cap never
  looked at must be visibly absent rather than missing;
* the old shape -- a caller who passes no range parameter must get back the
  response they got before this existed, key for key.
"""

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.fanout import PlanError, plan_hotel_stays
from tests.test_server import build_with_upstream

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def room(name: str, price: float | None, **extra) -> dict:
    """A property row shaped like the upstream's, image URL included."""
    row = {
        "name": name,
        "review_score": 8.4,
        "review_count": 701,
        "room_type": "Superior Double or Twin Room",
        "link": "https://www.booking.com/hotel/it/x.html?aid=304142&sid=" + "b" * 32,
        "image_url": "https://cf.bstatic.com/xdata/images/hotel/" + "c" * 60 + ".jpg",
    }
    if price is not None:
        row["price"] = price
        row["price_string"] = f"US${price:,.0f}"
        row["currency"] = "USD"
    row.update(extra)
    return row


async def call(mcp, tool: str, **kwargs):
    async with Client(mcp) as client:
        out = await client.call_tool(tool, kwargs)
        return out.structured_content


def recording_handler(price_for, *, remaining: str | None = "19990"):
    """A handler that records every stay asked for and prices it by callback.

    `price_for(checkin, checkout)` returns the rows for that stay, or raises
    `Boom` to make that one search fail the way a 5xx does.
    """
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        stay = {
            "checkin_date": body["checkin_date"],
            "checkout_date": body["checkout_date"],
        }
        rows = price_for(stay["checkin_date"], stay["checkout_date"])
        if rows is Boom:
            return httpx.Response(503, text="unavailable")
        seen.append(stay)
        headers = (
            {"x-ratelimit-requests-remaining": remaining} if remaining else {}
        )
        return httpx.Response(
            200, json={"properties": rows}, headers=headers
        )

    return handler, seen


class Boom:
    """Sentinel: make this stay's search fail."""


# ── the planner ──────────────────────────────────────────────────────────


class TestExpansion:
    def test_a_range_and_one_length_is_one_stay_per_date(self):
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-05",
            nights=3,
            cap=30,
        )
        assert plan.requested_combinations == 5
        assert plan.combos[0] == {
            "checkin_date": "2026-05-01",
            "checkout_date": "2026-05-04",
        }
        assert plan.combos[-1] == {
            "checkin_date": "2026-05-05",
            "checkout_date": "2026-05-08",
        }

    def test_several_lengths_multiply(self):
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-04",
            nights=[2, 7],
            cap=30,
        )
        assert plan.requested_combinations == 8
        # Date-major: sampling a truncated plan must spread across the
        # calendar, not across trip lengths on one day.
        assert [c["checkin_date"] for c in plan.combos[:2]] == [
            "2026-05-01",
            "2026-05-01",
        ]

    def test_one_date_and_several_lengths_needs_no_range(self):
        plan = plan_hotel_stays(
            checkin_date="2026-05-01", nights=[1, 2, 3], cap=30
        )
        assert [c["checkout_date"] for c in plan.combos] == [
            "2026-05-02",
            "2026-05-03",
            "2026-05-04",
        ]

    def test_a_fixed_checkout_against_a_range_skips_impossible_stays(self):
        """"Out on the 4th, whenever I arrive" is a real question. A checkout
        on or before the check-in is not a stay, and the upstream derives
        nights from the two dates -- so those pairs are dropped here rather
        than failing deep in someone else's stack."""
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-06",
            checkout_date="2026-05-04",
            cap=30,
        )
        assert [c["checkin_date"] for c in plan.combos] == [
            "2026-05-01",
            "2026-05-02",
            "2026-05-03",
        ]

    def test_nights_and_checkout_together_is_refused(self):
        with pytest.raises(PlanError, match="not both"):
            plan_hotel_stays(
                checkin_date="2026-05-01",
                checkout_date="2026-05-04",
                nights=3,
                cap=30,
            )

    def test_a_range_with_no_length_is_refused(self):
        with pytest.raises(PlanError, match="nights"):
            plan_hotel_stays(
                checkin_date_from="2026-05-01",
                checkin_date_to="2026-05-05",
                cap=30,
            )

    def test_half_a_range_names_the_missing_half(self):
        with pytest.raises(PlanError, match="checkin_date_to"):
            plan_hotel_stays(
                checkin_date_from="2026-05-01", nights=2, cap=30
            )

    def test_zero_nights_is_not_a_stay(self):
        with pytest.raises(PlanError, match="at least one night"):
            plan_hotel_stays(checkin_date="2026-05-01", nights=0, cap=30)

    def test_the_flight_wording_did_not_move(self):
        """The two axes share one resolver; the flights messages are what
        callers and tests already read."""
        from src.fanout import _resolve_departure_dates

        with pytest.raises(PlanError, match="a departure date is required"):
            _resolve_departure_dates(None, None, None)


class TestCapAndSampling:
    def test_the_cap_samples_evenly_and_keeps_both_ends(self):
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-31",
            nights=2,
            cap=5,
        )
        assert plan.requested_combinations == 31
        assert plan.executed_combinations == 5
        assert plan.truncated is True
        dates = [c["checkin_date"] for c in plan.combos]
        assert dates[0] == "2026-05-01"
        assert dates[-1] == "2026-05-31"
        # Spread, not the first five days.
        assert dates == sorted(dates)
        assert len(set(dates)) == 5

    def test_coverage_names_the_exact_stays_and_says_it_sampled(self):
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-10",
            nights=3,
            cap=4,
        )
        coverage = plan.coverage()
        assert coverage["requested_combinations"] == 10
        assert coverage["searched_combinations"] == 4
        assert coverage["truncated"] is True
        assert coverage["max_searches_per_request"] == 4
        assert len(coverage["stays_searched"]) == 4
        assert coverage["stays_searched"][0] == {
            "checkin_date": "2026-05-01",
            "checkout_date": "2026-05-04",
        }
        assert coverage["checkin_dates_searched"] == sorted(
            coverage["checkin_dates_searched"]
        )
        assert "sampled" not in coverage  # the fact lives in `truncated`
        assert "max_searches" in coverage["note"]
        assert "evenly" in coverage["note"]

    def test_an_untruncated_range_carries_no_note(self):
        plan = plan_hotel_stays(
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=1,
            cap=30,
        )
        coverage = plan.coverage()
        assert coverage["truncated"] is False
        assert "note" not in coverage

    def test_the_flight_coverage_shape_is_untouched(self):
        """One `coverage()` serves both axes; a stay key leaking into a
        flights response would be a silent contract change on a live tool."""
        from src.fanout import plan_oneway

        coverage = plan_oneway(
            from_airport="TLV",
            to_airport="BCN",
            departure_date_from="2026-05-01",
            departure_date_to="2026-05-03",
            cap=30,
        ).coverage()
        assert "departure_dates_searched" in coverage
        assert "stays_searched" not in coverage


# ── the tool ─────────────────────────────────────────────────────────────


class TestOneCallManyStays:
    @pytest.mark.asyncio
    async def test_one_backend_call_per_stay_with_the_right_dates(self):
        handler, seen = recording_handler(
            lambda ci, co: [room("Hotel " + ci, 300)]
        )
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-04",
            nights=2,
        )
        assert sorted(s["checkin_date"] for s in seen) == [
            "2026-05-01",
            "2026-05-02",
            "2026-05-03",
            "2026-05-04",
        ]
        assert all(
            s["checkout_date"]
            == f"2026-05-0{int(s['checkin_date'][-1]) + 2}"
            for s in seen
        )
        assert len(out["stays"]) == 4
        assert out["api_usage"]["requests_used_by_this_call"] == 4
        assert "one check-in date paired with one length" in (
            out["api_usage"]["note"]
        )

    @pytest.mark.asyncio
    async def test_the_search_parameters_travel_to_every_stay(self):
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"properties": []})

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-02",
            nights=2,
            price_as_seen_from="DE",
            filters=["free_cancellation"],
            budget_per_night=200,
            adults=3,
        )
        assert len(bodies) == 2
        for body in bodies:
            assert body["proxy_country"] == "DE"
            assert body["filters"] == ["free_cancellation"]
            assert body["budget_per_night"] == 200
            assert body["adults"] == 3

    @pytest.mark.asyncio
    async def test_cheapest_per_stay_and_overall(self):
        prices = {"2026-05-01": 400.0, "2026-05-02": 210.0, "2026-05-03": 330.0}

        def price_for(ci, _co):
            return [room("Cheap " + ci, prices[ci]), room("Pricey " + ci, 900.0)]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=3,
        )

        by_date = {s["checkin_date"]: s for s in out["stays"]}
        first = by_date["2026-05-01"]
        assert first["search_status"] == "ok"
        assert first["reason"] == "ok"
        assert first["nights"] == 3
        assert first["property_count"] == 2
        assert first["priced_count"] == 2
        assert first["cheapest_total"] == 400.0
        assert first["price_per_night"] == round(400 / 3, 2)
        assert first["median_total"] == 650.0
        assert first["currency"] == "USD"
        assert first["cheapest"]["name"] == "Cheap 2026-05-01"

        assert out["cheapest_overall"]["checkin_date"] == "2026-05-02"
        assert out["cheapest_overall"]["total"] == 210.0
        assert out["cheapest_overall"]["price_per_night"] == 70.0
        assert out["cheapest_overall"]["nights"] == 3
        assert out["search_status"] == "ok"

    @pytest.mark.asyncio
    async def test_full_rows_come_back_for_the_cheapest_stay_only(self):
        """The payload has to stay bounded. Measured on one real search: 25,892
        bytes for 25 properties, 18,989 of them URLs. Fifteen of those is not
        an answer a model reads, so every stay reports its cheapest and the
        winner reports everything."""

        def price_for(ci, _co):
            cheap = 200.0 if ci == "2026-05-02" else 500.0
            return [room("A " + ci, cheap), room("B " + ci, cheap + 100)]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=1,
        )
        assert out["results_for_stay"] == {
            "checkin_date": "2026-05-02",
            "checkout_date": "2026-05-03",
            "nights": 1,
        }
        assert [r["name"] for r in out["results"]] == ["A 2026-05-02", "B 2026-05-02"]
        assert out["result_count"] == 2
        # The winner's rows are the upstream's, untouched.
        assert "image_url" in out["results"][0]
        # The repeated per-stay summaries are not: an image CDN URL is the
        # half of those bytes no model can open.
        for stay in out["stays"]:
            assert "image_url" not in stay["cheapest"]
            assert stay["cheapest"]["link"].startswith("https://www.booking.com/")

    @pytest.mark.asyncio
    async def test_a_stay_the_cap_skipped_is_present_and_says_so(self):
        handler, seen = recording_handler(lambda ci, _co: [room("H " + ci, 300)])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-10",
            nights=2,
            max_searches=3,
        )
        assert len(seen) == 3
        assert len(out["stays"]) == 10, "every stay asked for has an entry"
        skipped = [s for s in out["stays"] if s["search_status"] == "not_searched"]
        assert len(skipped) == 7
        for stay in skipped:
            assert stay["reason"] == "not_searched"
            # Null, never zero: zero reads as "nothing there", which an
            # unsearched stay does not know.
            assert stay["property_count"] is None
            assert stay["priced_count"] is None
            assert stay["cheapest"] is None
        assert out["search_coverage"]["truncated"] is True
        assert len(out["search_coverage"]["stays_searched"]) == 3

    @pytest.mark.asyncio
    async def test_max_searches_cannot_exceed_the_deployment_cap(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(
            handler, fallback_rapidapi_key=KEY, max_searches_per_tool_call=4
        )
        await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-20",
            nights=1,
            max_searches=50,
        )
        assert len(seen) == 4

    @pytest.mark.asyncio
    async def test_max_searches_below_one_is_refused(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="at least 1"):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date_from": "2026-05-01",
                        "checkin_date_to": "2026-05-03",
                        "nights": 1,
                        "max_searches": 0,
                    },
                )
        assert seen == []


class TestTheHonestContractPerStay:
    @pytest.mark.asyncio
    async def test_a_failed_stay_is_degraded_not_empty(self):
        """The whole reason this file exists twice over. "Booking errored on
        the 2nd" and "the 2nd has no rooms" are opposite answers, and a model
        handed the second one tells somebody to travel on the 3rd."""

        def price_for(ci, _co):
            if ci == "2026-05-02":
                return Boom
            return [room("H " + ci, 300)]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=1,
        )
        by_date = {s["checkin_date"]: s for s in out["stays"]}
        assert by_date["2026-05-02"]["search_status"] == "degraded"
        assert by_date["2026-05-02"]["reason"] == "search_failed"
        assert by_date["2026-05-02"]["property_count"] is None
        assert by_date["2026-05-01"]["search_status"] == "ok"
        assert out["search_status"] == "partial"
        assert "degraded" in out["partial"]
        assert "no availability" in out["partial"]

    @pytest.mark.asyncio
    async def test_a_stay_with_no_properties_is_empty(self):
        def price_for(ci, _co):
            return [] if ci == "2026-05-02" else [room("H " + ci, 300)]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-02",
            nights=1,
        )
        by_date = {s["checkin_date"]: s for s in out["stays"]}
        assert by_date["2026-05-02"]["search_status"] == "empty"
        assert by_date["2026-05-02"]["reason"] == "no_availability"
        assert by_date["2026-05-02"]["property_count"] == 0
        assert by_date["2026-05-02"]["priced_count"] == 0

    @pytest.mark.asyncio
    async def test_properties_with_no_price_are_not_availability(self):
        """`available: false` on a named property comes back as a row with no
        price. Counting that as priced availability would put a null in
        `cheapest` and a real-looking stay in the answer."""

        def price_for(ci, _co):
            if ci == "2026-05-02":
                return [room("Sold out", None, available=False)]
            return [room("H " + ci, 300)]

        handler, _seen = recording_handler(price_for)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-02",
            nights=1,
        )
        by_date = {s["checkin_date"]: s for s in out["stays"]}
        assert by_date["2026-05-02"]["search_status"] == "empty"
        assert by_date["2026-05-02"]["reason"] == "no_price"
        assert by_date["2026-05-02"]["property_count"] == 1
        assert by_date["2026-05-02"]["priced_count"] == 0
        assert by_date["2026-05-02"]["cheapest"] is None

    @pytest.mark.asyncio
    async def test_nothing_priced_anywhere_says_so_out_loud(self):
        handler, _seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=1,
        )
        assert out["search_status"] == "empty"
        assert out["cheapest_overall"] is None
        assert out["results"] == []
        assert out["results_for_stay"] is None
        assert "No priced availability" in out["message"]

    @pytest.mark.asyncio
    async def test_every_stay_failing_is_an_error_not_an_empty_answer(self):
        handler, _seen = recording_handler(lambda _ci, _co: Boom)
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="temporarily unavailable"):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date_from": "2026-05-01",
                        "checkin_date_to": "2026-05-02",
                        "nights": 1,
                    },
                )

    @pytest.mark.asyncio
    async def test_an_unsubscribed_key_is_one_fact_not_n_failures(self):
        def handler(_r):
            return httpx.Response(403, json={"message": "You are not subscribed"})

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-05",
            nights=1,
        )
        assert out["needs_api_key"] is True
        assert "Subscribe" in out["message"]
        assert "stays" not in out

    @pytest.mark.asyncio
    async def test_no_key_spends_nothing(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key="")
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-05",
            nights=1,
        )
        assert out["needs_api_key"] is True
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_bad_filter_still_costs_nothing_on_a_range(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date_from": "2026-05-01",
                        "checkin_date_to": "2026-05-05",
                        "nights": 2,
                        "filters": ["free_cancelation"],
                    },
                )
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_range_on_two_sources_is_refused_rather_than_guessed(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="one source at a time"):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date_from": "2026-05-01",
                        "checkin_date_to": "2026-05-05",
                        "nights": 2,
                        "providers": ["booking", "airbnb"],
                    },
                )
        assert seen == []


class TestTheRateCalendarForOneProperty:
    @pytest.mark.asyncio
    async def test_one_property_priced_across_a_range(self):
        """One property x N stays is a rate calendar -- the thing rival hotel
        APIs ship for two chains and not for Booking."""
        prices = {"2026-05-01": 190.0, "2026-05-02": 150.0, "2026-05-03": 240.0}
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            # /hotel_by_name answers with ONE object, not a list.
            return httpx.Response(
                200, json=room("Hotel Artemide", prices[body["checkin_date"]])
            )

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "find_hotel_by_name",
            hotel_name="Hotel Artemide",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=2,
        )
        assert [b["hotel_name"] for b in bodies] == ["Hotel Artemide"] * 3
        assert len(out["stays"]) == 3
        assert [s["cheapest_total"] for s in out["stays"]] == [190.0, 150.0, 240.0]
        assert [s["price_per_night"] for s in out["stays"]] == [95.0, 75.0, 120.0]
        assert out["cheapest_overall"]["checkin_date"] == "2026-05-02"
        assert out["results"][0]["name"] == "Hotel Artemide"

    @pytest.mark.asyncio
    async def test_several_lengths_on_one_property(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            nights = (
                int(body["checkout_date"][-2:]) - int(body["checkin_date"][-2:])
            )
            return httpx.Response(200, json=room("Artemide", 100.0 * nights))

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "find_hotel_by_name",
            hotel_name="Artemide",
            checkin_date="2026-05-01",
            nights=[1, 2, 4],
        )
        assert [s["nights"] for s in out["stays"]] == [1, 2, 4]
        assert [s["price_per_night"] for s in out["stays"]] == [100.0, 100.0, 100.0]


class TestTheOldShapeIsUntouched:
    """A caller who passes no range parameter must get the response they got
    before this existed -- key for key, value for value. Every hotel caller on
    the server today is that caller."""

    HANDLER_JSON = {"properties": [room("Kremlin Palace", 2434.0)]}

    @pytest.mark.asyncio
    async def test_a_single_stay_search_is_byte_identical(self):
        def handler(_r):
            return httpx.Response(
                200,
                json=self.HANDLER_JSON,
                headers={
                    "x-ratelimit-requests-remaining": "19990",
                    "x-ratelimit-requests-limit": "20000",
                },
            )

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Antalya",
            checkin_date="2026-05-01",
            checkout_date="2026-05-10",
        )
        assert out == {
            "results": self.HANDLER_JSON["properties"],
            "result_count": 1,
            "api_usage": {
                "requests_used_by_this_call": 1,
                "plan_requests_remaining": 19990,
                "plan_requests_limit": 20000,
                "note": (
                    "This search used 1 of your RapidAPI plan's requests; "
                    "19990 of 20000 remain in the current period. Each hotel "
                    "search is one billed request; there is no fan-out."
                ),
            },
        }

    @pytest.mark.asyncio
    async def test_a_single_property_lookup_is_byte_identical(self):
        def handler(_r):
            return httpx.Response(
                200,
                json=room("Kremlin Palace", 2434.0),
                headers={"x-ratelimit-requests-remaining": "19990"},
            )

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "find_hotel_by_name",
            hotel_name="Kremlin Palace",
            checkin_date="2026-05-01",
            checkout_date="2026-05-10",
        )
        assert out == {
            "results": [room("Kremlin Palace", 2434.0)],
            "result_count": 1,
            "api_usage": {
                "requests_used_by_this_call": 1,
                "plan_requests_remaining": 19990,
                "note": (
                    "This search used 1 of your RapidAPI plan's requests. Each "
                    "hotel search is one billed request; there is no fan-out."
                ),
            },
        }

    @pytest.mark.asyncio
    async def test_the_single_stay_path_sends_exactly_one_request(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date="2026-05-01",
            checkout_date="2026-05-04",
        )
        assert seen == [
            {"checkin_date": "2026-05-01", "checkout_date": "2026-05-04"}
        ]

    @pytest.mark.asyncio
    async def test_missing_dates_name_both_ways_to_ask(self):
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="checkin_date_from"):
                await client.call_tool("search_hotels", {"destination": "Rome"})
        assert seen == []

    @pytest.mark.asyncio
    async def test_max_searches_on_a_single_stay_is_refused_not_ignored(self):
        """It would do nothing, and a parameter that silently does nothing is
        how a caller concludes the cap does not work."""
        handler, seen = recording_handler(lambda ci, _co: [])
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="check-in range"):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                        "max_searches": 5,
                    },
                )
        assert seen == []


class TestTheSchemaAndAnnotations:
    @pytest.mark.asyncio
    async def test_the_new_parameters_are_described_for_the_model(self):
        from src.schema_docs import undocumented_params

        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_hotels", "find_hotel_by_name"):
            schema = tools[name].inputSchema
            assert undocumented_params(schema) == []
            for param in ("checkin_date_from", "checkin_date_to", "nights"):
                assert param in schema["properties"]

    @pytest.mark.asyncio
    async def test_a_stay_search_is_never_idempotent(self):
        """A host that read idempotentHint True would be entitled to serve a
        cached rate calendar, which is how a stale price gets quoted to
        somebody about to book."""
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_hotels", "find_hotel_by_name"):
            assert tools[name].annotations.idempotentHint is False
            assert tools[name].annotations.readOnlyHint is True

    @pytest.mark.asyncio
    async def test_the_declared_schema_covers_what_a_range_returns(self):
        from jsonschema import Draft202012Validator

        handler, _seen = recording_handler(
            lambda ci, _co: ([room("H", 300.0)] if ci != "2026-05-02" else [])
        )
        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            schema = {
                t.name: t.outputSchema for t in await client.list_tools()
            }["search_hotels"]
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date_from": "2026-05-01",
                    "checkin_date_to": "2026-05-03",
                    "nights": 1,
                    "max_searches": 2,
                },
            )
        Draft202012Validator(schema).validate(out.structured_content)

    def test_the_reason_vocabulary_is_declared_once(self):
        from src import output_schema, server

        assert server.STAY_REASONS == output_schema.STAY_REASONS


class TestFiltersAreConfirmedOnARange:
    @pytest.mark.asyncio
    async def test_the_upstreams_applied_filters_survive_the_fanout(self):
        """A filtered search that silently returns unfiltered results is the
        worst outcome here -- somebody monitoring "free cancellation only"
        gets everything and does not know. The upstream's confirmation is the
        only evidence, so it has to survive the fan-out."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "properties": [room("H", 300.0)],
                    "applied_filters": ["free_cancellation"],
                },
            )

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        out = await call(
            mcp,
            "search_hotels",
            destination="Rome",
            checkin_date_from="2026-05-01",
            checkin_date_to="2026-05-03",
            nights=1,
            filters=["free_cancellation"],
        )
        assert out["applied_filters"] == ["free_cancellation"]
