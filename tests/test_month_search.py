"""
One call covers a whole month -- the cap, the pacing, and what it costs.

Why this file exists
--------------------
"Cheapest round trip to Rome any day in October, 3, 4 or 5 nights" is ONE
question and 93 date/night combinations. Until 2026-09-22 the server answered
it from 30 of them, evenly sampled, and said so in `search_coverage` -- which
is honest and is still the wrong answer: the cheapest of 30 days presented
against a question about 31 is a number the user will book on.

So the cap rises by itself to cover a request that size (AUTO_MAX_SEARCHES),
an explicit `max_searches` reaches 200, and two new things had to come with
that, both tested here:

* **a rate**, because 93 requests at twelve in flight is ~290 a minute and
  the caller's plan is rate limited per minute as well as per month;
* **a refusal**, because a free allowance that cannot pay for 93 searches
  must be told so before anything runs, not sampled down to what it can
  afford (src/quota_gate.py).

The integration test at the bottom is the one that answers Matan's question
directly: 93 combinations, a stubbed backend, one call, under budget.
"""

import asyncio
import json
import time

import httpx
import pytest

from src.fanout import (
    DEADLINE_REACHED,
    SearchPlan,
    execute_plan,
    plan_roundtrip,
)
from src.pacing import TokenBucket, bucket_for, reset_buckets
from src.quota_gate import QUOTA_EXCEEDED
from src.server import resolve_cap
from src.settings import (
    AUTO_MAX_SEARCHES,
    DEFAULT_HUB_REQUESTS_PER_MINUTE,
    DEFAULT_MAX_SEARCHES,
    HARD_MAX_SEARCHES,
    load_settings,
)
from tests.test_server import KEY, build_with_upstream, call

#: October 2026 in full, at three trip lengths, to one destination.
MONTH = dict(
    from_airport="TLV",
    to_airport="FCO",
    departure_date_from="2026-10-01",
    departure_date_to="2026-10-31",
    nights=[3, 4, 5],
)
MONTH_COMBOS = 31 * 3


def _cap(requested, max_searches=None, trial_remaining=None, default=None, auto=None):
    return resolve_cap(
        requested=requested,
        max_searches=max_searches,
        default_cap=DEFAULT_MAX_SEARCHES if default is None else default,
        auto_cap=AUTO_MAX_SEARCHES if auto is None else auto,
        hard_cap=HARD_MAX_SEARCHES,
        trial_remaining=trial_remaining,
    )


class TestTheCapPolicy:
    """`resolve_cap` on its own: three inputs, one number, and a reason."""

    def test_a_small_question_still_costs_the_default(self):
        assert _cap(12) == (DEFAULT_MAX_SEARCHES, "default")

    def test_a_month_at_three_trip_lengths_raises_the_cap_to_cover_it(self):
        assert _cap(MONTH_COMBOS) == (MONTH_COMBOS, "auto_span")

    def test_the_automatic_raise_never_exceeds_its_own_ceiling(self):
        # Three destinations across the month is 279; the raise stops at 100
        # and the plan is sampled from there, which the coverage says.
        assert _cap(279) == (AUTO_MAX_SEARCHES, "auto_span")

    def test_the_raise_is_bounded_by_what_was_actually_asked_for(self):
        """A 40-combination question costs 40, not the auto ceiling."""
        assert _cap(40) == (40, "auto_span")

    def test_an_explicit_value_raises_past_the_automatic_ceiling(self):
        assert _cap(279, max_searches=200) == (200, "explicit")

    def test_an_explicit_value_still_lowers(self):
        assert _cap(MONTH_COMBOS, max_searches=5) == (5, "explicit")

    def test_nothing_reaches_past_the_hard_maximum(self):
        assert _cap(5000, max_searches=5000) == (HARD_MAX_SEARCHES, "explicit")

    def test_a_trial_caller_gets_no_automatic_raise(self):
        assert _cap(MONTH_COMBOS, trial_remaining=10) == (10, "trial")

    def test_a_trial_caller_cannot_buy_the_raise_with_max_searches(self):
        assert _cap(MONTH_COMBOS, max_searches=93, trial_remaining=10) == (
            10,
            "trial",
        )

    def test_a_trial_caller_can_still_spend_less(self):
        """Spending less of our money never needs permission."""
        assert _cap(MONTH_COMBOS, max_searches=2, trial_remaining=10) == (
            2,
            "explicit",
        )

    def test_an_auto_ceiling_below_the_default_cannot_lower_the_cap(self):
        # The clamp holds the cap at the default and the SOURCE still
        # says the request was bigger than it -- which is the fact the
        # coverage note needs.
        assert _cap(50, default=30, auto=5) == (30, "auto_span")


class TestThePlanItself:
    def test_the_month_expands_to_ninety_three_combinations(self):
        plan = plan_roundtrip(**MONTH, cap=HARD_MAX_SEARCHES)
        assert plan.requested_combinations == MONTH_COMBOS
        assert plan.executed_combinations == MONTH_COMBOS
        assert plan.truncated is False

    def test_recap_re_samples_from_the_full_expansion_not_the_sample(self):
        """The planner runs once, against the ceiling, and is re-capped.

        The property that makes that safe: recapping twice is the same as
        capping once, because `requested_combos` is never sampled.
        """
        wide = plan_roundtrip(**MONTH, cap=HARD_MAX_SEARCHES)
        twice = wide.recap(60).recap(30)
        once = plan_roundtrip(**MONTH, cap=30)
        assert [c["departure_date"] for c in twice.combos] == [
            c["departure_date"] for c in once.combos
        ]
        assert twice.requested_combinations == MONTH_COMBOS

    def test_the_coverage_names_the_cap_and_why_it_is_that_number(self):
        plan = plan_roundtrip(**MONTH, cap=MONTH_COMBOS).recap(
            MONTH_COMBOS, cap_source="auto_span"
        )
        coverage = plan.coverage()
        assert coverage["requested_combinations"] == MONTH_COMBOS
        assert coverage["searched_combinations"] == MONTH_COMBOS
        assert coverage["truncated"] is False
        assert coverage["max_searches_per_request"] == MONTH_COMBOS
        assert coverage["max_searches_source"] == "auto_span"
        assert len(coverage["departure_dates_searched"]) == 31


class TestTheRate:
    """The pacer: a token bucket shared per KEY, not per fan-out.

    The first version spread the starts of ONE call, which is not the limit
    RapidAPI enforces -- two concurrent month searches on one key are two
    compliant fan-outs and 186 requests a minute between them.
    """

    @staticmethod
    def _plan(n):
        return SearchPlan(
            endpoint="oneway",
            combos=[{"departure_date": f"d{i}", "to_airport": "X"} for i in range(n)],
            requested_combinations=n,
            cap=n,
        )

    @staticmethod
    async def _noop(_endpoint, _payload):
        return []

    def setup_method(self):
        reset_buckets()

    def test_a_full_bucket_lets_a_whole_month_straight_through(self):
        """93 out of 120 tokens, no wait. The month pays nothing for this."""
        bucket = TokenBucket(DEFAULT_HUB_REQUESTS_PER_MINUTE)
        assert [bucket.take() for _ in range(93)] == [0.0] * 93

    def test_the_next_request_past_the_allowance_waits_for_a_refill(self):
        bucket = TokenBucket(120)
        for _ in range(120):
            bucket.take()
        wait = bucket.take()
        # 120/minute is two a second, so the 121st waits about half a second.
        assert 0.4 < wait < 0.6

    def test_the_bucket_is_shared_by_every_call_on_one_key(self):
        """The whole point of item 5: two fan-outs, one allowance."""
        first = bucket_for("key-AAA", 120)
        second = bucket_for("key-AAA", 120)
        assert first is second
        for _ in range(120):
            first.take()
        assert second.take() > 0

    def test_a_different_key_has_its_own_allowance(self):
        mine = bucket_for("key-AAA", 120)
        theirs = bucket_for("key-BBB", 120)
        assert mine is not theirs
        for _ in range(120):
            mine.take()
        assert theirs.take() == 0.0

    def test_the_key_itself_is_never_a_dictionary_key(self):
        from src import pacing

        bucket_for("super-secret-key", 120)
        assert "super-secret-key" not in pacing._BUCKETS
        assert all(len(k) == 32 for k in pacing._BUCKETS)

    def test_zero_disables_it(self):
        assert bucket_for("key-AAA", 0) is None
        assert TokenBucket(0).take() == 0.0

    @pytest.mark.asyncio
    async def test_a_month_through_a_shared_bucket_is_not_delayed(self):
        started = time.monotonic()
        out = await execute_plan(
            self._plan(93),
            build_payload=lambda c: c,
            run_search=self._noop,
            max_concurrency=12,
            pacer=bucket_for("key-AAA", DEFAULT_HUB_REQUESTS_PER_MINUTE),
        )
        assert out.backend_calls_made == 93
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_a_second_concurrent_month_queues_instead_of_bursting(self):
        """Two 93s on one key: the 120-token bucket makes the rest wait.

        Measured as delay rather than as a rate, because a rate needs a
        minute to observe. The bucket is sized down so the wait is short.
        """
        pacer = bucket_for("key-AAA", 60)  # one a second, 60 in the bucket
        started = time.monotonic()
        first, second = await asyncio.gather(
            execute_plan(
                self._plan(40),
                build_payload=lambda c: c,
                run_search=self._noop,
                max_concurrency=12,
                pacer=pacer,
            ),
            execute_plan(
                self._plan(40),
                build_payload=lambda c: c,
                run_search=self._noop,
                max_concurrency=12,
                pacer=pacer,
            ),
        )
        elapsed = time.monotonic() - started
        assert first.backend_calls_made == 40
        assert second.backend_calls_made == 40
        # 80 requests out of a 60-token bucket refilling at one a second:
        # the last twenty wait, so this cannot finish instantly.
        assert elapsed > 15

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded_whatever_the_plan_size(self):
        peak = {"now": 0, "max": 0}

        async def run(_endpoint, _payload):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.01)
            peak["now"] -= 1
            return []

        await execute_plan(
            self._plan(93),
            build_payload=lambda c: c,
            run_search=run,
            max_concurrency=12,
        )
        assert peak["max"] <= 12


class TestTheWallClock:
    """`deadline_seconds`: stop DISPATCHING, never cancel, report partial.

    Without it a 200-combination plan against a slow upstream runs past the
    function's own 300s maxDuration: the caller is billed for ~180 searches
    and gets a dead connection.
    """

    @staticmethod
    def _plan(n):
        return SearchPlan(
            endpoint="oneway",
            combos=[{"departure_date": f"d{i}", "to_airport": "X"} for i in range(n)],
            requested_combinations=n,
            cap=n,
        )

    @pytest.mark.asyncio
    async def test_a_slow_upstream_stops_dispatching_and_returns_what_ran(self):
        sent = []

        async def slow(_endpoint, payload):
            sent.append(payload)
            await asyncio.sleep(0.25)
            return [{"price": "$1", "price_as_number": 1}]

        out = await execute_plan(
            self._plan(60),
            build_payload=lambda c: c,
            run_search=slow,
            max_concurrency=4,
            deadline_seconds=0.6,
        )
        # 4 in flight × 0.25s: ~8-12 go out in 0.6s, nowhere near 60.
        assert 0 < len(sent) < 60
        assert out.backend_calls_made == len(sent)
        assert out.stopped_reason == DEADLINE_REACHED
        assert len(out.skipped_combos) == 60 - len(sent)
        # The fares that DID come back are kept: they are already billed.
        assert out.results
        assert len(out.results_by_combo) == len(sent)

    @pytest.mark.asyncio
    async def test_nothing_already_in_flight_is_cancelled(self):
        """A sent request is billed; throwing its answer away is pure loss."""
        finished = []

        async def slow(_endpoint, payload):
            await asyncio.sleep(0.5)
            finished.append(payload)
            return [{"price": "$1", "price_as_number": 1}]

        out = await execute_plan(
            self._plan(8),
            build_payload=lambda c: c,
            run_search=slow,
            max_concurrency=8,
            deadline_seconds=0.1,
        )
        # All eight were dispatched before the clock ran out, and all eight
        # were waited for rather than abandoned.
        assert len(finished) == 8
        assert out.backend_calls_made == 8
        assert out.stopped_reason is None

    @pytest.mark.asyncio
    async def test_zero_disables_it(self):
        async def slow(_endpoint, _payload):
            await asyncio.sleep(0.05)
            return []

        out = await execute_plan(
            self._plan(20),
            build_payload=lambda c: c,
            run_search=slow,
            max_concurrency=2,
            deadline_seconds=0,
        )
        assert out.backend_calls_made == 20
        assert out.stopped_reason is None

    def test_the_plan_narrowed_to_what_ran_reads_as_truncated(self):
        plan = plan_roundtrip(**MONTH, cap=HARD_MAX_SEARCHES)
        ran = plan.combos[:12]
        narrowed = plan.narrowed_to(ran, DEADLINE_REACHED)
        coverage = narrowed.coverage()
        assert coverage["requested_combinations"] == MONTH_COMBOS
        assert coverage["searched_combinations"] == 12
        assert coverage["truncated"] is True
        assert coverage["stopped_early"] == DEADLINE_REACHED
        # NOT the cap's wording: the cap had nothing to do with it.
        assert "time limit" in coverage["note"]
        assert "cap" not in coverage["note"]


class TestTheDeploymentDefaults:
    def test_the_shipped_cap_covers_a_whole_month(self):
        settings = load_settings("flights")
        assert settings.max_searches_per_tool_call == DEFAULT_MAX_SEARCHES
        # The number Matan asked for: a whole month at three trip lengths,
        # in one call, with no argument from the model.
        assert settings.auto_max_searches >= MONTH_COMBOS
        assert HARD_MAX_SEARCHES >= 2 * MONTH_COMBOS
        assert settings.max_concurrent_searches <= settings.max_http_connections

    def test_the_shipped_rate_is_under_the_pro_limit_and_really_paces(self):
        """Asserts the RATE, not just that a number is configured.

        The old version of this test read `0 < n < 150` and would have
        passed against a pacer that was never consulted.
        """
        settings = load_settings("flights")
        assert 0 < settings.hub_requests_per_minute < 150  # PRO is 150/min
        reset_buckets()
        bucket = bucket_for("shipped-key", settings.hub_requests_per_minute)
        assert bucket is not None
        taken = 0
        while bucket.take() == 0.0:
            taken += 1
            assert taken <= settings.hub_requests_per_minute + 1
        # The bucket handed out its whole allowance and then said wait --
        # which is what "120 a minute" means.
        assert taken == settings.hub_requests_per_minute

    def test_the_shipped_deadline_leaves_room_under_maxduration(self):
        import json
        import pathlib

        settings = load_settings("flights")
        vercel = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "vercel.json").read_text()
        )
        max_duration = vercel["functions"]["api/index.py"]["maxDuration"]
        assert 0 < settings.fanout_deadline_seconds < max_duration
        # Enough headroom for the slowest in-flight search to land and the
        # response to be built: a deadline at maxDuration protects nothing.
        assert max_duration - settings.fanout_deadline_seconds >= 30


class TestTheWholeMonthThroughTheTool:
    """One tool call, 93 combinations, a stubbed backend."""

    @staticmethod
    def _upstream(seen, remaining=None):
        def handler(request):
            body = request.read().decode()
            seen.append(body)
            price = 200 + len(seen)
            headers = {}
            if remaining is not None:
                headers = {
                    "x-ratelimit-requests-limit": "10",
                    "x-ratelimit-requests-remaining": str(
                        max(0, remaining - len(seen))
                    ),
                }
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "price": f"${price}",
                        "price_as_number": price,
                        "total_price_as_number": price,
                        "airline": "Test Air",
                        "stops": 0,
                        "duration": "4 hr",
                        "to_airport": "FCO",
                        # Unique per request: the merge deduplicates on
                        # the booking link, so 93 identical rows would
                        # arrive as one and the fan-out would look broken.
                        "buy_link": f"https://www.google.com/travel/flights/{len(seen)}",
                    }
                ],
            )

        return handler

    @pytest.mark.asyncio
    async def test_a_whole_month_runs_in_one_call_and_says_what_it_cost(self):
        seen = []
        mcp = build_with_upstream(
            self._upstream(seen),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
            max_concurrent_searches=12,
        )
        started = time.monotonic()
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)
        elapsed = time.monotonic() - started

        assert len(seen) == MONTH_COMBOS
        coverage = out["search_coverage"]
        assert coverage["requested_combinations"] == MONTH_COMBOS
        assert coverage["searched_combinations"] == MONTH_COMBOS
        assert coverage["truncated"] is False
        assert coverage["max_searches_source"] == "auto_span"
        assert out["api_usage"]["requests_used_by_this_call"] == MONTH_COMBOS
        # One row per combination reaches the caller: the auto-raised `limit`
        # has to keep up with the auto-raised cap, or 33 searches that ran
        # and answered would have nothing in `results`.
        assert out["result_count"] == MONTH_COMBOS
        # Sorted by price, still.
        prices = [r["price_as_number"] for r in out["results"]]
        assert prices == sorted(prices)
        # Nowhere near the 300s function budget with a stub upstream.
        assert elapsed < 30

    @pytest.mark.asyncio
    async def test_the_default_cap_still_binds_a_question_that_fits_it(self):
        """The raise is for requests that are big, not for every request."""
        seen = []
        mcp = build_with_upstream(
            self._upstream(seen),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(
            mcp,
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date_from="2026-10-01",
            departure_date_to="2026-10-05",
            nights=[3, 4],
            limit=50,
        )
        assert len(seen) == 10
        assert out["search_coverage"]["max_searches_source"] == "default"
        assert out["search_coverage"]["truncated"] is False


class TestTheRefusalOnAPlanThatCannotPay:
    """A key whose remaining quota cannot cover the fan-out.

    Detected from RapidAPI's `x-ratelimit-requests-remaining` header, which
    exists only on a response -- so the fan-out spends ONE request to read
    it and stops there. See src/quota_gate.py.
    """

    @pytest.mark.asyncio
    async def test_a_free_plan_is_refused_after_one_request_not_sampled(self):
        seen = []
        mcp = build_with_upstream(
            TestTheWholeMonthThroughTheTool._upstream(seen, remaining=10),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)

        assert len(seen) == 1, "the gate must stop the fan-out, not run it"
        assert out["search_status"] == QUOTA_EXCEEDED
        # The one combination that read the quota was a REAL search and is
        # already billed, so its fares come back with the refusal. Throwing
        # them away was the refusal doing, in miniature, the thing it exists
        # to prevent: charge for a search and hand back nothing.
        assert out["results"], "the probe's fares are paid for and must be kept"
        assert out["result_count"] == len(out["results"])
        assert out["combos_searched"] == 1
        assert "cover a single date out of the range" in out["message"]
        assert out["combos_requested"] == MONTH_COMBOS
        assert out["combos_allowed_now"] == 9
        assert out["remaining_month"] == 9
        assert out["requests_spent"] == 1
        assert "needs 93 requests" in out["message"]
        assert "9 left this month" in out["message"]
        assert "rapidapi" in out["message"].lower()

    @pytest.mark.asyncio
    async def test_a_plan_with_room_is_never_refused(self):
        """The gate is "cannot pay", not "asked for a lot"."""
        seen = []
        mcp = build_with_upstream(
            TestTheWholeMonthThroughTheTool._upstream(seen, remaining=2500),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)
        assert len(seen) == MONTH_COMBOS
        assert out.get("search_status") != QUOTA_EXCEEDED
        assert out["result_count"] == MONTH_COMBOS

    @pytest.mark.asyncio
    async def test_a_gateway_that_sends_no_quota_headers_is_not_refused(self):
        """Unknown is not "no". A caller is never refused on a guess."""
        seen = []
        mcp = build_with_upstream(
            TestTheWholeMonthThroughTheTool._upstream(seen),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)
        assert len(seen) == MONTH_COMBOS
        assert out.get("search_status") != QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_a_small_search_is_never_gated_and_costs_no_extra_trip(self):
        """Below the default cap the gate is off: nothing to protect, and a
        serial first request would be latency every ordinary search pays."""
        seen = []
        mcp = build_with_upstream(
            TestTheWholeMonthThroughTheTool._upstream(seen, remaining=1),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(
            mcp,
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport="FCO",
            departure_date_from="2026-10-01",
            departure_date_to="2026-10-03",
            nights=3,
            limit=50,
        )
        assert len(seen) == 3
        assert out.get("search_status") != QUOTA_EXCEEDED


class TestWhatTheHubActuallyBills:
    """Retries are billed. Coverage that counts combinations does not see it.

    `rapidapi_client._RETRYABLE_STATUS` retries a 5xx once, deliberately and
    unchanged here. What changes is that the second request is COUNTED: a
    fan-out of 93 over a flaky window can cost 130 requests, and reporting
    93 as the bill is a number the caller's invoice contradicts.
    """

    @staticmethod
    def _flaky(seen, fail_first_n):
        """Every one of the first `fail_first_n` combinations 503s once."""
        combos: dict[str, int] = {}

        def handler(request):
            body = json.loads(request.read().decode())
            key = f"{body['departure_date']}|{body.get('return_date')}"
            combos[key] = combos.get(key, 0) + 1
            seen.append(key)
            first_time = combos[key] == 1
            if first_time and len(combos) <= fail_first_n:
                return httpx.Response(503, json={"message": "upstream hiccup"})
            price = 200 + len(combos)
            return httpx.Response(
                200,
                headers={
                    "x-ratelimit-requests-limit": "50000",
                    "x-ratelimit-requests-remaining": "45000",
                },
                json=[
                    {
                        "price": f"${price}",
                        "price_as_number": price,
                        "total_price_as_number": price,
                        "airline": "Test Air",
                        "stops": 0,
                        "duration": "4 hr",
                        "to_airport": "FCO",
                        "buy_link": f"https://www.google.com/travel/flights/{key}",
                    }
                ],
            )

        return handler

    @pytest.mark.asyncio
    async def test_retries_are_counted_and_reported_apart_from_coverage(self):
        seen = []
        mcp = build_with_upstream(
            self._flaky(seen, fail_first_n=5),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)

        usage = out["api_usage"]
        # 93 combinations, 5 of them sent twice.
        assert out["search_coverage"]["searched_combinations"] == MONTH_COMBOS
        assert usage["requests_used_by_this_call"] == MONTH_COMBOS
        assert usage["hub_requests_billed"] == MONTH_COMBOS + 5 == len(seen)
        assert "used 98 of your RapidAPI plan's requests" in usage["note"]
        assert "5 of them were automatic retries" in usage["note"]

    @pytest.mark.asyncio
    async def test_a_clean_run_says_the_same_number_twice(self):
        """No retries, no extra sentence -- the note reads as it always did."""
        seen = []
        mcp = build_with_upstream(
            self._flaky(seen, fail_first_n=0),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
        )
        out = await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)
        usage = out["api_usage"]
        assert usage["hub_requests_billed"] == usage["requests_used_by_this_call"]
        assert "automatic retries" not in usage["note"]


class TestTheQuotaRunningOutMidBurst:
    """429 on SOME combinations after the fan-out is already running.

    The gate cannot see this one: it reads the quota off the first response,
    and a plan with room then runs out because another client on the same
    key spent it. The fares already paid for are kept (that part shipped),
    but the status stayed "ok" beside a coverage note saying half the range
    was missing -- so a client branching on the status alone, which is what
    a status is for, read it as a complete answer.
    """

    @staticmethod
    def _dies_after(n):
        sent = {"n": 0}

        def handler(request):
            sent["n"] += 1
            if sent["n"] > n:
                return httpx.Response(
                    429,
                    headers={
                        "x-ratelimit-requests-limit": "2500",
                        "x-ratelimit-requests-remaining": "0",
                    },
                    json={"message": "You have exceeded the MONTHLY quota"},
                )
            price = 200 + sent["n"]
            return httpx.Response(
                200,
                headers={
                    "x-ratelimit-requests-limit": "2500",
                    "x-ratelimit-requests-remaining": str(2500 - sent["n"]),
                },
                json=[
                    {
                        "price": f"${price}",
                        "price_as_number": price,
                        "total_price_as_number": price,
                        "airline": "Test Air",
                        "stops": 0,
                        "duration": "4 hr",
                        "to_airport": "FCO",
                        "buy_link": f"https://www.google.com/travel/flights/{sent['n']}",
                    }
                ],
            )

        return handler

    async def _run(self, dies_after):
        mcp = build_with_upstream(
            self._dies_after(dies_after),
            fallback_rapidapi_key=KEY,
            max_searches_per_tool_call=DEFAULT_MAX_SEARCHES,
            auto_max_searches=AUTO_MAX_SEARCHES,
            max_concurrent_searches=1,  # deterministic ordering
        )
        return await call(mcp, "search_roundtrip_flights", **MONTH, limit=200)

    @pytest.mark.asyncio
    async def test_the_fares_already_paid_for_are_kept(self):
        out = await self._run(40)
        assert out["result_count"] > 0
        assert out["search_coverage"]["quota_exhausted_mid_search"] is True
        assert "ran out of requests part-way" in out["search_coverage"]["note"]

    @pytest.mark.asyncio
    async def test_the_status_is_partial_not_ok(self):
        """The fix. A half-covered range must not answer `ok`."""
        out = await self._run(40)
        assert out["search_status"] == "partial"

    @pytest.mark.asyncio
    async def test_the_partial_line_names_the_quota_as_the_reason(self):
        out = await self._run(40)
        assert "searches failed" in out["partial"]
        assert "ran out of requests part-way through" in out["partial"]

    @pytest.mark.asyncio
    async def test_a_plan_that_dies_on_the_very_first_call_is_a_refusal(self):
        """Nothing answered, so there is nothing to keep.

        The 429 itself reports `remaining: 0`, so the gate sees the truth on
        the first response and refuses with the numbers rather than letting
        92 more requests go out against an empty plan.
        """
        out = await self._run(0)
        assert out["search_status"] == QUOTA_EXCEEDED
        assert out["results"] == []
        assert out["result_count"] == 0
        assert out["combos_searched"] == 0
        assert out["combos_allowed_now"] == 0
        assert "0 left this month" in out["message"]
