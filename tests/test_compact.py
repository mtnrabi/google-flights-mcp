"""The result has to be small enough for a host to put in the conversation.

Found on claude.ai on 2026-09-22 and measured the same day against the live
paid server: a 279-combination round-trip month search answered with about
2.4MB of tool result (1.2MB of `structuredContent` and the same again as the
serialized text mirror). claude.ai refused to inject it -- "Tool result too
large for context, stored at /mnt/user-data/tool_results/…" -- and handed the
model a FILE instead. The model read the file and answered correctly, so the
only visible symptom was the MCP-UI card sitting on "Loading fares…" for ever:
the card is fed from the injected result, and there was no injected result.

Measured, real keyed calls, TLV -> FCO[,ATH], 1-31 October, nights 3/4/5:

    93 combinations    401,684 B structured + the same again as text
    186 combinations   803,061 B structured + the same again as text

Linear in combinations, so 279 is ~1.2MB + ~1.2MB. After the change in
src/compact.py the same three responses are 52.2KB, 55.7KB and 59.0KB of
tool result in total -- 15x, 29x and 40x smaller, and 13x under the smallest
result claude.ai is known to have rendered.

What these tests hold in place is the ARITHMETIC of that, not the byte
counts, which move with every fare: the bound applies to rows the caller did
not ask for and not to rows they did, `by_destination` survives the bound so
"which destination is cheapest" stays answerable, nothing is silently
dropped without `results_total` saying so, and `verbose: true` gets all of it
back.
"""

import json

import httpx
import pytest

from src.compact import (
    SUMMARY_DESTINATIONS,
    bound_rows,
    compact_row,
    destination_of,
    nights_between,
    summary_text,
)
from src.status_text import serialize_payload
from tests.test_server import build_with_upstream, call, call_result

DATES = [f"2026-10-{day:02d}" for day in range(1, 32)]
DESTINATIONS = ["FCO", "ATH", "BUD"]
#: 31 dates x 3 destinations. The shape of the search that broke.
COMBINATIONS = len(DATES) * len(DESTINATIONS)

ONEWAY_UPSTREAM_ROW = {
    "price_range_in_relation_to_other_periods": "low",
    "price_insights_low": 145,
    "price_insights_high": 205,
    "from_airport": "Tel Aviv (TLV)",
    "to_airport": "Barcelona (BCN)",
    "departure_date": "2026-10-04",
    "price": "$129",
    "price_as_number": 129,
    "duration": "4 hr 40 min",
    "duration_seconds": 16800,
    "buy_link": "https://www.google.com/travel/flights?tfs=abc&curr=usd",
    "airline": "Bluebird Airways",
    "stops": 0,
    "stops_info": [],
    "departure_description": "3:50 PM on Sun, Oct 4",
    "arrival_description": "7:30 PM on Sun, Oct 4",
}

ROUNDTRIP_UPSTREAM_ROW = {
    "price_range_in_relation_to_other_periods": "typical",
    "price_insights_low": 125,
    "price_insights_high": 260,
    "from_airport": "Tel Aviv (TLV)",
    "to_airport": "Rome (FCO)",
    "departure_date": "2026-10-23",
    "return_date": "2026-10-28",
    "total_price": "$128",
    "total_price_as_number": 128,
    "total_duration_seconds": 26100,
    "total_stops": 0,
    "buy_link": "https://www.google.com/travel/flights?tfs=def&curr=usd",
    "departure_flight_departure_description": "7:40 PM on Fri, Oct 23",
    "departure_flight_arrival_description": "10:30 PM on Fri, Oct 23",
    "departure_flight_airline": "Wizz Air",
    "departure_flight_stops": 0,
    "departure_flight_duration": "3 hr 50 min",
    "departure_stops_info": [],
    "return_flight_departure_description": "5:45 AM on Wed, Oct 28",
    "return_flight_arrival_description": "10:10 AM on Wed, Oct 28",
    "return_flight_airline": "Wizz Air",
    "return_flight_stops": 0,
    "return_flight_duration": "3 hr 25 min",
    "return_stops_info": [],
}

#: Fields the upstream sends that a compact row drops. Named rather than
#: derived, so adding one to the keep list has to be a deliberate edit here.
DROPPED_ONEWAY = ("from_airport", "duration_seconds", "stops_info", "arrival_description")
DROPPED_ROUNDTRIP = (
    "from_airport",
    "total_duration_seconds",
    "departure_flight_arrival_description",
    "departure_flight_stops",
    "departure_stops_info",
    "return_flight_arrival_description",
    "return_flight_stops",
    "return_stops_info",
)


def month_upstream(rows_per_combo: int = 3):
    """One priced row per requested trip length, per date, per destination."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dest = body["to_airport"]
        day = body["departure_date"]
        back = body.get("return_date")
        # Wide gaps between destinations on purpose: the bound must drop
        # whole destinations out of `results` for the by_destination tests
        # below to mean anything.
        base = 100 + DESTINATIONS.index(dest) * 200 + DATES.index(day)
        rows = []
        for offset in range(rows_per_combo):
            if back:
                row = dict(
                    ROUNDTRIP_UPSTREAM_ROW,
                    to_airport=dest,
                    departure_date=day,
                    return_date=back,
                    total_price=f"${base + offset}",
                    total_price_as_number=base + offset,
                    buy_link=f"https://book/{dest}/{day}/{back}/{offset}",
                )
            else:
                row = dict(
                    ONEWAY_UPSTREAM_ROW,
                    to_airport=dest,
                    departure_date=day,
                    price=f"${base + offset}",
                    price_as_number=base + offset,
                    buy_link=f"https://book/{dest}/{day}/{offset}",
                )
            rows.append(row)
        return httpx.Response(200, json=rows, headers={"X-Search-Status": "ok"})

    return handler


def build(**overrides):
    overrides.setdefault("max_searches_per_tool_call", COMBINATIONS)
    overrides.setdefault("auto_max_searches", COMBINATIONS)
    return build_with_upstream(month_upstream(), **overrides)


MONTH_ONEWAY = {
    "from_airport": "TLV",
    "to_airport": DESTINATIONS,
    "departure_date_from": DATES[0],
    "departure_date_to": DATES[-1],
    "sort_by": "price",
}


class TestTheRowShape:
    def test_a_oneway_row_keeps_what_is_read_and_drops_what_is_not(self):
        row = compact_row(ONEWAY_UPSTREAM_ROW, roundtrip=False)

        for field in ("price", "price_as_number", "buy_link", "airline", "stops",
                      "duration", "departure_date", "to_airport",
                      "departure_description", "price_insights_low",
                      "price_insights_high",
                      "price_range_in_relation_to_other_periods"):
            assert row[field] == ONEWAY_UPSTREAM_ROW[field]
        for field in DROPPED_ONEWAY:
            assert field not in row

    def test_a_roundtrip_row_keeps_both_legs_and_drops_the_rest(self):
        row = compact_row(ROUNDTRIP_UPSTREAM_ROW, roundtrip=True)

        for field in ("total_price", "total_price_as_number", "total_stops",
                      "buy_link", "departure_date", "return_date",
                      "departure_flight_airline", "departure_flight_duration",
                      "departure_flight_departure_description",
                      "return_flight_airline", "return_flight_duration",
                      "return_flight_departure_description"):
            assert row[field] == ROUNDTRIP_UPSTREAM_ROW[field]
        for field in DROPPED_ROUNDTRIP:
            assert field not in row

    def test_a_roundtrip_row_carries_its_trip_length(self):
        """The one field ADDED. A `nights` fan-out prices the same departure
        date at three lengths; without this the model has to subtract two
        dates on every row to answer the question it was asked."""
        assert compact_row(ROUNDTRIP_UPSTREAM_ROW, roundtrip=True)["nights"] == 5

    @pytest.mark.parametrize(
        "out,back,expected",
        [
            ("2026-10-01", "2026-10-04", 3),
            ("2026-10-01", "2026-10-01", 0),
            ("2026-10-05", "2026-10-01", None),
            ("2026-10-01", "", None),
            ("not-a-date", "2026-10-04", None),
            (None, "2026-10-04", None),
        ],
    )
    def test_trip_length_never_guesses(self, out, back, expected):
        assert nights_between(out, back) is expected or nights_between(out, back) == expected

    def test_a_fare_survives_under_either_upstream_name(self):
        """`price` on a one-way, `total_price` on a round trip -- and this
        fixture sends both, which is what the by-destination fixtures do.
        A row that lost its fare to a naming guess is an empty price column."""
        both = dict(ONEWAY_UPSTREAM_ROW, total_price="$129", total_price_as_number=129)
        for roundtrip in (True, False):
            row = compact_row(both, roundtrip=roundtrip)
            assert row["price_as_number"] == 129
            assert row["total_price_as_number"] == 129


class TestTheRowBound:
    @pytest.mark.asyncio
    async def test_a_month_wide_search_answers_with_the_cheapest_sixty(self):
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        assert data["results_total"] == COMBINATIONS
        assert data["results_returned"] == 60
        assert data["result_count"] == 60
        assert len(data["results"]) == 60
        prices = [row["price_as_number"] for row in data["results"]]
        assert prices == sorted(prices)

    @pytest.mark.asyncio
    async def test_the_bound_says_so_in_the_coverage_note(self):
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        note = data["search_coverage"]["note"]
        assert "60" in note and str(COMBINATIONS) in note
        assert "verbose" in note

    @pytest.mark.asyncio
    async def test_a_limit_the_caller_asked_for_is_answered_in_full(self):
        """The bound exists for rows the SERVER added. `_effective_limit`
        raises `limit` by itself to cover the fan-out; an explicit `limit`
        that already covers it was a decision, and decisions are honoured."""
        data = await call(
            build(), "search_oneway_flights", **MONTH_ONEWAY, limit=COMBINATIONS
        )

        assert data["results_returned"] == data["results_total"] == COMBINATIONS
        assert len(data["results"]) == COMBINATIONS

    @pytest.mark.asyncio
    async def test_a_small_search_is_untouched(self):
        data = await call(
            build(),
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date=DATES[0],
        )

        assert data["results_total"] == data["results_returned"] == 3
        assert len(data["results"]) == 3

    @pytest.mark.asyncio
    async def test_result_rows_max_zero_is_the_rollback_switch(self):
        """One env var puts the whole pre-2026-09-22 shape back: every row,
        every field, `by_destination.rows` and all. The bound and the row
        compaction share the switch on purpose -- a rollback that restored
        the count but not the fields would be half a rollback."""
        data = await call(
            build(result_rows_max=0), "search_oneway_flights", **MONTH_ONEWAY
        )

        assert data["results_returned"] == data["results_total"] == COMBINATIONS
        for field in DROPPED_ONEWAY:
            assert field in data["results"][0]
        assert "rows" in data["by_destination"]["FCO"]

    @pytest.mark.asyncio
    async def test_verbose_returns_every_row_and_every_field(self):
        data = await call(
            build(), "search_oneway_flights", **MONTH_ONEWAY, verbose=True
        )

        assert data["results_returned"] == data["results_total"] == COMBINATIONS
        for field in DROPPED_ONEWAY:
            assert field in data["results"][0]


class TestNothingIsLostToTheBound:
    @pytest.mark.asyncio
    async def test_no_destination_is_bounded_out_of_the_results(self):
        """The regression a head slice would ship.

        Budapest is 400 dearer than Rome on every date in this fixture, so
        the 60 cheapest rows of 93 are ~31 Rome, ~29 Athens and NO Budapest
        -- a destination that was searched, billed and answered, missing
        from `results`. The card builds its filter chips from the rows it is
        handed (src/widget.py `bucketsOf`), so it would draw two chips for a
        three-destination question. `bound_rows` reserves per destination.
        """
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        shown = [row["to_airport"] for row in data["results"]]
        assert set(shown) == set(DESTINATIONS), "a destination was bounded out"
        # What the card would draw: one chip per bucket, in first-seen order.
        assert len(dict.fromkeys(shown)) == 3
        # And the split is even rather than "whatever sorted first".
        for dest in DESTINATIONS:
            assert shown.count(dest) == 20

    @pytest.mark.asyncio
    async def test_it_is_still_each_destinations_own_best_rows(self):
        """Reserving per destination must not reorder anything: what each
        destination contributes is its own cheapest, and the merged list
        stays in sort order."""
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        prices = [row["price_as_number"] for row in data["results"]]
        assert prices == sorted(prices)
        for dest in DESTINATIONS:
            mine = [r["price_as_number"] for r in data["results"] if r["to_airport"] == dest]
            floor = 100 + DESTINATIONS.index(dest) * 200
            assert mine == sorted(mine)
            assert mine[0] == floor, "not that destination's cheapest fare"

    @pytest.mark.asyncio
    async def test_every_destination_still_has_its_own_cheapest_fare(self):
        """`by_destination` is built before the bound, so it answers "which
        destination is cheapest" even for one the bound could not fit."""
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        for dest in DESTINATIONS:
            entry = data["by_destination"][dest]
            assert entry["reason"] == "ok"
            assert entry["cheapest"]["price_as_number"] is not None
            assert entry["row_count"] == len(DATES)

    @pytest.mark.asyncio
    async def test_more_destinations_than_the_bound_still_shares_it_out(self):
        """61 buckets and 60 rows cannot all fit. What must not happen is
        the first bucket taking 60 of them."""
        rows = [{"to_airport": f"D{i:02d}", "price_as_number": i} for i in range(61)]
        rows += [{"to_airport": "D00", "price_as_number": 100 + i} for i in range(200)]
        kept = bound_rows(rows, 60)

        assert len(kept) == 60
        assert len({row["to_airport"] for row in kept}) == 60

    def test_a_row_with_no_destination_is_its_own_bucket(self):
        """The card's "Other" chip. Lumping it in with the first destination
        would filter it out of every view of the card."""
        rows = [{"to_airport": "FCO", "price_as_number": i} for i in range(10)]
        rows += [{"price_as_number": 500}]
        kept = bound_rows(rows, 4)

        assert any(destination_of(row) == "" for row in kept)

    @pytest.mark.asyncio
    async def test_the_origin_is_on_the_response_not_on_every_row(self):
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        assert data["from_airport"] == "Tel Aviv (TLV)"
        assert all("from_airport" not in row for row in data["results"])


class TestTheDuplicateCopies:
    @pytest.mark.asyncio
    async def test_by_destination_does_not_repeat_the_rows(self):
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        for entry in data["by_destination"].values():
            assert "rows" not in entry

    @pytest.mark.asyncio
    async def test_a_nights_fanout_counts_each_row_once(self):
        """The regression this found. `_combo_key` is (destination, departure
        date), so three trip lengths put the same key in the requested list
        three times and `by_destination` walked each row once per copy: a
        measured 93-row answer carried 279 rows there, every one of them
        three times, ~300KB of duplicate JSON."""
        data = await call(
            build(),
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport=["FCO"],
            departure_date_from=DATES[0],
            departure_date_to=DATES[-1],
            nights=[3, 4, 5],
            sort_by="price",
            verbose=True,
        )

        rows = data["by_destination"]["FCO"]["rows"]
        assert len(rows) == data["results_total"] == len(data["results"])
        assert len({row["buy_link"] for row in rows}) == len(rows)

    @pytest.mark.asyncio
    async def test_a_large_result_is_not_mirrored_as_json_as_well(self):
        # An explicit bound rather than the shipped 48,000: these fixture
        # rows carry two-character booking links and 60 of them are a third
        # of the size 60 real ones are, so the default would not be reached
        # and the test would pass without exercising anything.
        result = await call_result(
            build(text_mirror_max_bytes=8_000),
            "search_oneway_flights",
            **MONTH_ONEWAY,
        )

        assert len(result.content) == 1
        text = result.content[0].text
        assert len(text) < 2_000, "the mirror came back"
        assert "structuredContent" in text
        assert str(result.structured_content["results_total"]) in text
        # The data is still all there -- only the duplicate went.
        assert len(result.structured_content["results"]) == 60

    @pytest.mark.asyncio
    async def test_a_small_result_keeps_its_json_mirror_byte_for_byte(self):
        """Most calls are small and the spec asks for the serialized copy.
        Only the results that were too big to inject lose it."""
        result = await call_result(
            build(),
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date=DATES[0],
        )

        assert [c.text for c in result.content] == [
            serialize_payload(result.structured_content)
        ]

    @pytest.mark.asyncio
    async def test_the_summary_names_the_cheapest_fares(self):
        result = await call_result(
            build(text_mirror_max_bytes=8_000),
            "search_oneway_flights",
            **MONTH_ONEWAY,
        )

        cheapest = result.structured_content["results"][0]
        assert cheapest["price"] in result.content[0].text
        assert cheapest["to_airport"] in result.content[0].text

    def test_the_summary_is_honest_about_what_it_left_out(self):
        payload = {
            "results": [compact_row(ONEWAY_UPSTREAM_ROW, roundtrip=False)],
            "results_returned": 1,
            "results_total": 279,
            "search_coverage": {
                "searched_combinations": 279,
                "requested_combinations": 279,
                "departure_dates_searched": DATES,
                "destinations_searched": DESTINATIONS,
            },
        }
        text = summary_text(payload, omitted_bytes=58_991)

        assert "1 of 279" in text
        assert "279 date/destination combinations searched of 279" in text
        assert f"departure dates {DATES[0]} to {DATES[-1]}" in text
        assert "58,991" in text
        assert "structuredContent" in text

    @pytest.mark.asyncio
    async def test_the_summary_names_every_destinations_cheapest(self):
        """A host that shows the model only the text block sees THIS. Three
        fares off the top of the list would all be Rome, and the question
        was which of three destinations is cheapest."""
        result = await call_result(
            build(text_mirror_max_bytes=8_000),
            "search_oneway_flights",
            **MONTH_ONEWAY,
        )
        text = result.content[0].text

        assert "Cheapest per destination:" in text
        for dest in DESTINATIONS:
            assert dest in text
        assert text.count("\n- ") == len(DESTINATIONS)
        assert "Coverage:" in text

    def test_the_summary_stops_at_ten_destinations_and_says_so(self):
        payload = {
            "results": [],
            "results_returned": 60,
            "results_total": 400,
            "by_destination": {
                f"D{i:02d}": {
                    "cheapest": {"to_airport": f"D{i:02d}", "price_as_number": i,
                                 "price": f"${i}", "departure_date": DATES[0]},
                    "row_count": 1, "searched": True, "reason": "ok",
                }
                for i in range(14)
            },
        }
        text = summary_text(payload, omitted_bytes=1)

        assert text.count("\n- ") == SUMMARY_DESTINATIONS + 1
        assert "and 4 more destinations" in text


class TestEveryExitPathIsCounted:
    """`results_total` / `results_returned` are on the output schema, so a
    client that reads them must find them on the replies that are not
    searches at all."""

    @pytest.mark.asyncio
    async def test_a_keyless_reply_carries_them(self):
        data = await call(
            build(fallback_rapidapi_key=""),
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date=DATES[0],
        )

        assert data["needs_api_key"] is True
        assert data["results"] == []
        assert data["results_total"] == data["results_returned"] == 0

    @pytest.mark.asyncio
    async def test_an_exhausted_plan_carries_them(self):
        def refuse(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, json={"message": "You have exceeded the MONTHLY quota"}
            )

        data = await call(
            build_with_upstream(refuse),
            "search_oneway_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date=DATES[0],
        )

        assert data["quota_exhausted"] is True
        assert data["results_total"] == data["results_returned"] == 0


class TestTheSortIsNotAlwaysPrice:
    @pytest.mark.asyncio
    async def test_the_note_does_not_call_the_shortest_flights_the_cheapest(self):
        data = await call(
            build(), "search_oneway_flights", **{**MONTH_ONEWAY, "sort_by": "duration"}
        )

        note = data["search_coverage"]["note"]
        assert "duration (shortest first)" in note
        assert "cheapest 60" not in note

    @pytest.mark.asyncio
    async def test_a_price_sort_still_says_cheapest(self):
        data = await call(build(), "search_oneway_flights", **MONTH_ONEWAY)

        assert "price (cheapest first)" in data["search_coverage"]["note"]


class TestTheSizeItself:
    """The point of the whole change, asserted as a bound rather than a
    number: fares move, and a test that pinned 52,233 bytes would fail on a
    day Rome got dearer."""

    @pytest.mark.asyncio
    async def test_a_month_over_three_destinations_fits_in_a_context(self):
        result = await call_result(build(), "search_oneway_flights", **MONTH_ONEWAY)

        total = len(serialize_payload(result.structured_content).encode()) + sum(
            len(block.text.encode()) for block in result.content
        )
        assert total < 120_000, f"tool result is {total:,} bytes"
