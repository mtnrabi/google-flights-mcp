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
an explicit `max_searches` reaches the same ceiling, and two new things had to
come with that, both tested here:

* **a rate**, because 93 requests at twelve in flight is ~290 a minute and
  the caller's plan is rate limited per minute as well as per month;
* **a refusal**, because a free allowance that cannot pay for 93 searches
  must be told so before anything runs, not sampled down to what it can
  afford (src/quota_gate.py).

The integration test at the bottom is the one that answers Matan's question
directly: 93 combinations, a stubbed backend, one call, under budget.

2026-09-22, same day, second question: "what about multiple destinations
support?" A month x 3 nights x THREE destinations is 31 x 3 x 3 = 279
combinations, which the 100 auto cap sampled to a third of itself and which
an explicit `max_searches` could not reach either at a hard cap of 200. Both
are 300 now, and the two things that had to move with them are pinned below:
the deadline (280s of a 300s function, because 279 combinations spend ~80s of
wall clock on the pacer alone) and the pacer itself, which is NOT raised --
120 a minute is what keeps a PRO key under its own 150/min limit.
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
from src.pacing import (
    DEFAULT_HUB_BURST_CAPACITY,
    TokenBucket,
    bucket_for,
    reset_buckets,
)
from src.quota_gate import QUOTA_EXCEEDED
from src.server import resolve_cap
from src.settings import (
    AUTO_MAX_SEARCHES,
    DEFAULT_HUB_BURST_CAPACITY as SETTINGS_BURST_CAPACITY,
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

#: The same month and trip lengths over THREE destinations: 279
#: combinations, the question Matan asked about on 2026-09-22 and the one
#: the 300 cap exists for.
MONTH3 = dict(MONTH, to_airport=["FCO", "ATH", "BUD"])
MONTH3_COMBOS = 31 * 3 * 3


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

    def test_a_month_at_three_nights_across_three_destinations_runs_in_full(self):
        """279 combinations, no `max_searches`, nothing sampled.

        This is the whole point of the 2026-09-22 raise: at the old auto
        ceiling of 100 the answer to "Rome, Athens or Budapest, any day in
        October, 3-5 nights" was the cheapest of a third of the grid,
        presented against a question about all of it.
        """
        assert _cap(MONTH3_COMBOS) == (MONTH3_COMBOS, "auto_span")
        assert MONTH3_COMBOS == 279

    def test_the_automatic_raise_never_exceeds_its_own_ceiling(self):
        # Two months across three destinations is past even the raised
        # ceiling; the raise stops at it and the plan is sampled from
        # there, which the coverage says.
        assert _cap(600) == (AUTO_MAX_SEARCHES, "auto_span")
        assert AUTO_MAX_SEARCHES == 300

    def test_the_raise_is_bounded_by_what_was_actually_asked_for(self):
        """A 40-combination question costs 40, not the auto ceiling."""
        assert _cap(40) == (40, "auto_span")

    def test_an_explicit_value_raises_past_the_default(self):
        """The automatic raise is bounded by the REQUEST; an explicit value
        is not, which is how a caller buys headroom for a grid the planner
        cannot know is coming."""
        assert _cap(20, max_searches=250) == (250, "explicit")
        assert _cap(MONTH3_COMBOS, max_searches=300) == (300, "explicit")

    def test_an_explicit_value_still_lowers(self):
        assert _cap(MONTH_COMBOS, max_searches=5) == (5, "explicit")

    def test_nothing_reaches_past_the_hard_maximum(self):
        assert _cap(5000, max_searches=5000) == (HARD_MAX_SEARCHES, "explicit")

    def test_a_trial_caller_gets_no_automatic_raise(self):
        assert _cap(MONTH_COMBOS, trial_remaining=10) == (10, "trial")

    def test_a_trial_caller_is_not_raised_to_three_hundred_either(self):
        """The 300 cap is a decision to spend more of the CALLER'S money.
        On a trial the money is ours, so the default is the ceiling and the
        day's remaining allowance lowers it further."""
        assert _cap(MONTH3_COMBOS, trial_remaining=10) == (10, "trial")
        assert _cap(MONTH3_COMBOS, max_searches=300, trial_remaining=10) == (
            10,
            "trial",
        )
        assert _cap(MONTH3_COMBOS, trial_remaining=10_000) == (
            DEFAULT_MAX_SEARCHES,
            "trial",
        )

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

    def test_three_destinations_expand_to_two_hundred_and_seventy_nine(self):
        plan = plan_roundtrip(**MONTH3, cap=HARD_MAX_SEARCHES)
        assert plan.requested_combinations == MONTH3_COMBOS
        assert plan.executed_combinations == MONTH3_COMBOS
        assert plan.truncated is False
        assert plan.requested_destinations == ["FCO", "ATH", "BUD"]
        assert len(plan.requested_departure_dates) == 31

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

    The bucket has TWO numbers, and the pair is the safety property: its
    worst rolling minute is `capacity + per_minute`, so 120 and 120 allowed
    240 -- above a PRO key's own 150/min, from the gate whose whole job is
    to stay under it. 30 + 120 = 150 exactly, and 30 is the default search
    cap, so an ordinary call still waits for nothing.
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

    def test_the_worst_rolling_minute_is_capacity_plus_refill(self):
        """The arithmetic the whole split exists for, stated once.

        A bucket can empty instantly and then take everything the refill
        hands out, so its ceiling over any 60 seconds is the SUM. The old
        bucket's sum was 240 against a PRO key's 150.
        """
        assert TokenBucket(120, 120).worst_minute == 240
        assert TokenBucket(120, 30).worst_minute == 150

    def test_the_shipped_pair_never_exceeds_the_pro_limit(self):
        bucket = TokenBucket(
            DEFAULT_HUB_REQUESTS_PER_MINUTE, DEFAULT_HUB_BURST_CAPACITY
        )
        assert bucket.worst_minute <= 150, "a PRO key is rate limited at 150/min"

    def test_the_burst_is_the_default_search_cap(self):
        """That equality is what makes the bound free: an ordinary call is
        capped at 30 combinations, so an ordinary call empties into a full
        bucket and waits for nothing at all. The two constants live in two
        modules (pacing imports no settings), so they are pinned here."""
        assert DEFAULT_HUB_BURST_CAPACITY == DEFAULT_MAX_SEARCHES
        assert SETTINGS_BURST_CAPACITY == DEFAULT_HUB_BURST_CAPACITY

    def test_a_default_sized_call_goes_straight_through(self):
        """30 out of 30 tokens, no wait. An ordinary call pays nothing."""
        bucket = TokenBucket(
            DEFAULT_HUB_REQUESTS_PER_MINUTE, DEFAULT_HUB_BURST_CAPACITY
        )
        assert [bucket.take() for _ in range(DEFAULT_MAX_SEARCHES)] == [
            0.0
        ] * DEFAULT_MAX_SEARCHES

    def test_the_next_request_past_the_burst_waits_for_a_refill(self):
        bucket = TokenBucket(120, 30)
        for _ in range(30):
            bucket.take()
        wait = bucket.take()
        # 120/minute is two a second, so the 31st waits about half a second.
        assert 0.4 < wait < 0.6

    def test_the_bucket_is_shared_by_every_call_on_one_key(self):
        """The whole point of item 5: two fan-outs, one allowance."""
        first = bucket_for("key-AAA", 120, 30)
        second = bucket_for("key-AAA", 120, 30)
        assert first is second
        for _ in range(30):
            first.take()
        assert second.take() > 0

    def test_a_changed_rate_rebuilds_the_bucket_too(self):
        """Capacity alone used to decide this, so a deployment that moved
        the RATE and left the capacity kept the old rate until eviction."""
        first = bucket_for("key-AAA", 120, 30)
        assert bucket_for("key-AAA", 60, 30) is not first

    def test_a_different_key_has_its_own_allowance(self):
        mine = bucket_for("key-AAA", 120, 30)
        theirs = bucket_for("key-BBB", 120, 30)
        assert mine is not theirs
        for _ in range(30):
            mine.take()
        assert theirs.take() == 0.0

    def test_the_key_itself_is_never_a_dictionary_key(self):
        from src import pacing

        bucket_for("super-secret-key", 120, 30)
        assert "super-secret-key" not in pacing._BUCKETS
        assert all(len(k) == 32 for k in pacing._BUCKETS)

    def test_zero_disables_it(self):
        assert bucket_for("key-AAA", 0) is None
        assert TokenBucket(0).take() == 0.0

    @pytest.mark.asyncio
    async def test_a_default_sized_call_through_a_shared_bucket_is_not_delayed(self):
        started = time.monotonic()
        out = await execute_plan(
            self._plan(DEFAULT_MAX_SEARCHES),
            build_payload=lambda c: c,
            run_search=self._noop,
            max_concurrency=12,
            pacer=bucket_for(
                "key-AAA", DEFAULT_HUB_REQUESTS_PER_MINUTE, DEFAULT_HUB_BURST_CAPACITY
            ),
        )
        assert out.backend_calls_made == DEFAULT_MAX_SEARCHES
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_a_second_concurrent_month_queues_instead_of_bursting(self):
        """Two 93s on one key: the 120-token bucket makes the rest wait.

        Measured as delay rather than as a rate, because a rate needs a
        minute to observe. The bucket is sized down so the wait is short.
        """
        pacer = bucket_for("key-AAA", 60, 60)  # one a second, 60 in the bucket
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


#: How much faster the simulated clock runs than the real one. The claim
#: under test is about 279 requests over a couple of MINUTES, which is not a
#: thing to make a test suite sit through; ×100 makes it ~1s of real time
#: while every number the code sees -- the pacer's waits, the deadline, the
#: per-search latency -- is the real one.
CLOCK_SCALE = 100.0


class _ScaledClock:
    """`time.monotonic`, run fast. Substituted for the MODULE reference.

    Patching the real `time.monotonic` would speed up pytest's own timers
    as well; replacing `src.fanout.time` and `src.pacing.time` with this
    changes the clock for exactly the two modules under test.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def monotonic(self) -> float:
        return (time.monotonic() - self._t0) * CLOCK_SCALE


class _ScaledAsyncio:
    """`asyncio`, with `sleep` reading the same fast clock.

    `execute_plan` asks the pacer how long to wait IN SECONDS and hands
    that number to `asyncio.sleep`. With a scaled clock those two have to
    agree or the pacer's 0.5s waits become 0.5s of real waiting and the
    simulation is no faster than the thing it simulates.
    """

    Semaphore = asyncio.Semaphore
    gather = staticmethod(asyncio.gather)

    @staticmethod
    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds / CLOCK_SCALE)


class TestTheThreeDestinationMonthAgainstTheClock:
    """279 combinations, 2s a search, 12 in flight, 120 a minute, 280s.

    The four numbers that have to fit together for the 300 cap to be safe,
    simulated rather than asserted from arithmetic -- the arithmetic is what
    said 93 combinations would take 20s and needed a pacer, and the pacer is
    what makes the 279 case slow enough to need a longer deadline. Running
    them together is the only way to know they still add up.
    """

    @pytest.mark.asyncio
    async def test_all_two_hundred_and_seventy_nine_run_inside_the_deadline(
        self, monkeypatch
    ):
        settings = load_settings("flights")
        clock = _ScaledClock()
        monkeypatch.setattr("src.fanout.time", clock)
        monkeypatch.setattr("src.pacing.time", clock)
        monkeypatch.setattr("src.fanout.asyncio", _ScaledAsyncio)

        reset_buckets()
        pacer = bucket_for(
            "three-dest-month",
            settings.hub_requests_per_minute,
            settings.hub_burst_capacity,
        )
        # Every moment the GATE let a request through. That is where the
        # bucket's guarantee is exact; `starts` below is where the request
        # actually went out, which the 12-slot semaphore can re-bunch.
        grants: list[float] = []

        class _Recording:
            """`TokenBucket` uses __slots__, so the probe wraps rather than
            patches. `execute_plan` only ever calls `.take()`."""

            def __init__(self, inner):
                self.inner = inner

            def take(self):
                wait = self.inner.take()
                if wait == 0.0:
                    grants.append(clock.monotonic())
                return wait

        pacer = _Recording(pacer)

        plan = plan_roundtrip(**MONTH3, cap=HARD_MAX_SEARCHES)
        assert plan.executed_combinations == MONTH3_COMBOS

        in_flight = 0
        peak = 0
        starts: list[float] = []

        async def two_seconds(_endpoint, payload):
            nonlocal in_flight, peak
            starts.append(clock.monotonic())
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(2.0 / CLOCK_SCALE)
            finally:
                in_flight -= 1
            return [{"price": "$1", "price_as_number": 1}]

        began = clock.monotonic()
        out = await execute_plan(
            plan,
            build_payload=lambda c: c,
            run_search=two_seconds,
            max_concurrency=settings.max_concurrent_searches,
            pacer=pacer,
            deadline_seconds=settings.fanout_deadline_seconds,
        )
        elapsed = clock.monotonic() - began

        # Nothing sampled, nothing skipped, nothing cut short.
        assert out.backend_calls_made == MONTH3_COMBOS
        assert out.stopped_reason is None
        assert out.skipped_combos == []
        assert len(out.results_by_combo) == MONTH3_COMBOS

        # Inside the shipped deadline, with room.
        assert elapsed < settings.fanout_deadline_seconds, (
            f"279 combinations took {elapsed:.0f}s of a "
            f"{settings.fanout_deadline_seconds:.0f}s deadline"
        )

        # ...and the pacer really is what it spent that time on. 279 starts
        # against a burst of 30 refilling at 2/s cannot be quicker than
        # (279 - 30) / 2 = ~125s however fast the upstream answers.
        floor = (MONTH3_COMBOS - settings.hub_burst_capacity) / (
            settings.hub_requests_per_minute / 60
        )
        assert elapsed > floor * 0.8, (
            f"{elapsed:.0f}s is under the pacer's own floor of {floor:.0f}s, "
            "so the pacer was not consulted"
        )

        # The busiest rolling minute, MEASURED -- the assertion the whole
        # capacity/refill split exists for.
        #
        # A token bucket empties instantly and then takes everything the
        # refill hands out, so its ceiling over any 60 seconds is
        # `capacity + per_minute`. Tied together at 120 and 120 that was
        # 240, and this same run measured 239 -- above a PRO key's own
        # 150/min, from the gate whose entire job is to stay under it.
        # At 30 + 120 it cannot pass 150.
        #
        # Measured on the GRANTS, which is where the bucket's guarantee
        # lives and where it is exact. `starts` -- when each search reached
        # the stub -- is a looser thing: a token taken while all twelve
        # slots are busy is spent later, so under load the starts bunch
        # behind the grants and the count over a 60s window drifts. A
        # review run caught exactly that (169 against a 162 bound), so the
        # start-time check below is a sanity bound, not the claim.
        def busiest(times: list[float]) -> int:
            times = sorted(times)
            worst = 0
            for i, start in enumerate(times):
                j = i
                while j < len(times) and times[j] < start + 60.0:
                    j += 1
                worst = max(worst, j - i)
            return worst

        ceiling = settings.hub_burst_capacity + settings.hub_requests_per_minute
        assert ceiling <= 150, "the pair must not exceed a PRO key's 150/min"
        assert len(grants) == MONTH3_COMBOS
        # THE CLAIM: no rolling minute ever let more than 150 searches out
        # of the gate.
        assert busiest(grants) <= ceiling, (
            f"{busiest(grants)} searches were let through inside one minute, "
            f"past the bucket's capacity-plus-refill ceiling of {ceiling}"
        )
        assert busiest(grants) <= 150
        # A sanity bound on when they actually reached the upstream: the
        # semaphore can delay a granted search and bunch it with later
        # ones, so this is deliberately loose. It exists to catch a pacer
        # that was bypassed entirely, not to restate the line above.
        assert busiest(starts) <= ceiling + 2 * settings.max_concurrent_searches

        assert peak <= settings.max_concurrent_searches

    @pytest.mark.asyncio
    async def test_an_upstream_slow_enough_to_run_out_of_clock_stops_dispatching(
        self, monkeypatch
    ):
        """The other side of the same arithmetic: the deadline still bites.

        At 20s a search the 279-combination plan cannot finish inside 280s
        whatever the pacer does, and what must NOT happen is the function
        being killed mid-response with ~200 requests billed. Everything that
        did not go out comes back as skipped and the plan reads truncated.
        """
        settings = load_settings("flights")
        clock = _ScaledClock()
        monkeypatch.setattr("src.fanout.time", clock)
        monkeypatch.setattr("src.pacing.time", clock)
        monkeypatch.setattr("src.fanout.asyncio", _ScaledAsyncio)

        reset_buckets()
        pacer = bucket_for(
            "slow-month",
            settings.hub_requests_per_minute,
            settings.hub_burst_capacity,
        )
        plan = plan_roundtrip(**MONTH3, cap=HARD_MAX_SEARCHES)

        sent = 0

        async def twenty_seconds(_endpoint, _payload):
            nonlocal sent
            sent += 1
            await asyncio.sleep(20.0 / CLOCK_SCALE)
            return []

        out = await execute_plan(
            plan,
            build_payload=lambda c: c,
            run_search=twenty_seconds,
            max_concurrency=settings.max_concurrent_searches,
            pacer=pacer,
            deadline_seconds=settings.fanout_deadline_seconds,
        )
        assert out.stopped_reason == DEADLINE_REACHED
        assert 0 < out.backend_calls_made < MONTH3_COMBOS
        assert out.backend_calls_made == sent
        assert len(out.skipped_combos) == MONTH3_COMBOS - sent
        narrowed = plan.narrowed_to(
            [combo for combo, _rows in out.results_by_combo], DEADLINE_REACHED
        )
        assert narrowed.truncated is True
        assert narrowed.coverage()["stopped_early"] == DEADLINE_REACHED


class TestTheDeploymentDefaults:
    def test_the_shipped_cap_covers_a_whole_month_over_three_destinations(self):
        settings = load_settings("flights")
        assert settings.max_searches_per_tool_call == DEFAULT_MAX_SEARCHES
        # The number Matan asked for: a whole month at three trip lengths
        # across three destinations, in one call, with no argument from the
        # model.
        assert settings.auto_max_searches >= MONTH3_COMBOS
        assert HARD_MAX_SEARCHES >= MONTH3_COMBOS
        assert settings.max_concurrent_searches <= settings.max_http_connections

    def test_the_shipped_rate_is_under_the_pro_limit_and_really_paces(self):
        """Asserts the RATE, not just that a number is configured.

        The old version of this test read `0 < n < 150` and would have
        passed against a pacer that was never consulted.
        """
        settings = load_settings("flights")
        assert 0 < settings.hub_requests_per_minute < 150  # PRO is 150/min
        # The BOUND, not just the rate: burst + refill is what a rolling
        # minute can hold, and it must not pass the Hub's own limit.
        assert (
            settings.hub_burst_capacity + settings.hub_requests_per_minute <= 150
        )
        reset_buckets()
        bucket = bucket_for(
            "shipped-key",
            settings.hub_requests_per_minute,
            settings.hub_burst_capacity,
        )
        assert bucket is not None
        assert bucket.worst_minute <= 150
        taken = 0
        while bucket.take() == 0.0:
            taken += 1
            assert taken <= settings.hub_burst_capacity + 1
        # The bucket handed out its whole burst and then said wait -- which
        # is what "30 at once, then 120 a minute" means.
        assert taken == settings.hub_burst_capacity
        assert taken == DEFAULT_MAX_SEARCHES, (
            "the burst is the default search cap, so a default-sized call "
            "never waits"
        )

    def test_the_shipped_deadline_leaves_room_under_maxduration(self):
        import json
        import pathlib

        settings = load_settings("flights")
        vercel = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "vercel.json").read_text()
        )
        max_duration = vercel["functions"]["api/index.py"]["maxDuration"]
        assert 0 < settings.fanout_deadline_seconds < max_duration
        # It is a DISPATCH deadline, so the headroom is for the searches
        # already in flight to land and the response to be built. Measured
        # tails are 1.9-5.5s; 20s is several times that. A deadline AT
        # maxDuration would protect nothing.
        assert max_duration - settings.fanout_deadline_seconds >= 20
        # ...and it has to be long enough for the plan the 300 cap allows:
        # 279 combinations spend (279 - 30) / (120/60) = ~125s on the pacer
        # before a single slow upstream second is counted.
        pacer_floor = (MONTH3_COMBOS - settings.hub_burst_capacity) / (
            settings.hub_requests_per_minute / 60
        )
        assert 120 < pacer_floor < 130, pacer_floor
        assert settings.fanout_deadline_seconds > pacer_floor * 2


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
