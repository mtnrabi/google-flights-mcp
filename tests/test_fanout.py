"""Fan-out planning: the cost cap is the thing most worth testing here."""

import asyncio

import pytest

from src.fanout import (
    PlanError,
    evenly_sample,
    execute_plan,
    expand_date_range,
    normalise_destinations,
    plan_oneway,
    plan_roundtrip,
)


class TestEvenlySample:
    def test_returns_everything_when_it_fits(self):
        assert evenly_sample([1, 2, 3], 5) == [1, 2, 3]

    def test_keeps_first_and_last(self):
        picked = evenly_sample(list(range(31)), 15)
        assert len(picked) == 15
        assert picked[0] == 0
        assert picked[-1] == 30

    def test_spreads_rather_than_truncating(self):
        # The whole point: 15 of October must not be Oct 1-15.
        picked = evenly_sample(list(range(31)), 15)
        assert max(picked) > 20, "sample must reach the end of the range"
        gaps = [b - a for a, b in zip(picked, picked[1:])]
        assert max(gaps) - min(gaps) <= 1, "spacing should be near-uniform"

    def test_preserves_order(self):
        picked = evenly_sample(list(range(100)), 9)
        assert picked == sorted(picked)

    def test_always_returns_exactly_k(self):
        for n in range(2, 40):
            for k in range(1, n):
                assert len(evenly_sample(list(range(n)), k)) == k

    def test_single_and_zero(self):
        assert evenly_sample([1, 2, 3], 1) == [1]
        assert evenly_sample([1, 2, 3], 0) == []


class TestDateRange:
    def test_inclusive(self):
        days = expand_date_range("2026-10-01", "2026-10-03")
        assert days == ["2026-10-01", "2026-10-02", "2026-10-03"]

    def test_rejects_reversed(self):
        with pytest.raises(PlanError, match="before its start"):
            expand_date_range("2026-10-05", "2026-10-01")

    def test_rejects_absurd_span(self):
        with pytest.raises(PlanError, match="maximum"):
            expand_date_range("2026-01-01", "2027-12-31")

    def test_rejects_garbage(self):
        with pytest.raises(PlanError, match="ISO date"):
            expand_date_range("next tuesday", "2026-10-01")


class TestDestinations:
    def test_accepts_string_list_and_csv(self):
        assert normalise_destinations("cmb") == ["CMB"]
        assert normalise_destinations(["cmb", "dxb"]) == ["CMB", "DXB"]
        assert normalise_destinations("cmb, dxb") == ["CMB", "DXB"]

    def test_dedupes_preserving_order(self):
        assert normalise_destinations(["DXB", "CMB", "dxb"]) == ["DXB", "CMB"]

    def test_rejects_empty(self):
        with pytest.raises(PlanError):
            normalise_destinations([])


class TestPlanOneway:
    def test_single_date_single_destination(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date="2026-10-14", cap=15,
        )
        assert plan.executed_combinations == 1
        assert not plan.truncated
        assert plan.coverage()["truncated"] is False

    def test_the_thirty_call_scenario_is_capped(self):
        # "oneway from TLV to Sri Lanka anywhere in October" -- the exact case
        # that would otherwise be 31 backend calls against one ad.
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date_from="2026-10-01", departure_date_to="2026-10-31",
            cap=15,
        )
        assert plan.requested_combinations == 31
        assert plan.executed_combinations == 15
        assert plan.truncated

    def test_cap_holds_across_multiple_destinations(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport=["CMB", "DXB", "BKK"],
            departure_date_from="2026-10-01", departure_date_to="2026-10-31",
            cap=15,
        )
        assert plan.requested_combinations == 93
        assert plan.executed_combinations == 15

    def test_coverage_reports_the_truncation(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date_from="2026-10-01", departure_date_to="2026-10-31",
            cap=15,
        )
        coverage = plan.coverage()
        assert coverage["requested_combinations"] == 31
        assert coverage["searched_combinations"] == 15
        assert "note" in coverage
        assert "spread evenly" in coverage["note"]
        assert len(coverage["departure_dates_searched"]) == 15

    def test_rejects_both_single_and_range(self):
        with pytest.raises(PlanError, match="not both"):
            plan_oneway(
                from_airport="TLV", to_airport="CMB",
                departure_date="2026-10-14",
                departure_date_from="2026-10-01",
                departure_date_to="2026-10-31",
                cap=15,
            )

    def test_rejects_half_a_range(self):
        with pytest.raises(PlanError, match="both"):
            plan_oneway(
                from_airport="TLV", to_airport="CMB",
                departure_date_from="2026-10-01", cap=15,
            )

    def test_rejects_no_date(self):
        with pytest.raises(PlanError, match="departure date is required"):
            plan_oneway(from_airport="TLV", to_airport="CMB", cap=15)


class TestPlanRoundtrip:
    def test_nights_derives_return_dates(self):
        plan = plan_roundtrip(
            from_airport="TLV", to_airport="FCO",
            departure_date="2026-05-01", nights=7, cap=15,
        )
        assert plan.combos[0]["return_date"] == "2026-05-08"

    def test_nights_list_multiplies_combinations(self):
        plan = plan_roundtrip(
            from_airport="TLV", to_airport="FCO",
            departure_date_from="2026-05-01", departure_date_to="2026-05-31",
            nights=[5, 6, 7], cap=15,
        )
        assert plan.requested_combinations == 93
        assert plan.executed_combinations == 15

    def test_requires_return_date_or_nights(self):
        with pytest.raises(PlanError, match="return_date"):
            plan_roundtrip(
                from_airport="TLV", to_airport="FCO",
                departure_date="2026-05-01", cap=15,
            )

    def test_rejects_both_return_date_and_nights(self):
        with pytest.raises(PlanError, match="not both"):
            plan_roundtrip(
                from_airport="TLV", to_airport="FCO",
                departure_date="2026-05-01",
                return_date="2026-05-08", nights=7, cap=15,
            )

    def test_skips_return_before_departure(self):
        # The backend computes nights from the two dates and a negative value
        # fails deep in the stack, so impossible pairs are dropped here.
        plan = plan_roundtrip(
            from_airport="TLV", to_airport="FCO",
            departure_date_from="2026-05-01", departure_date_to="2026-05-10",
            return_date="2026-05-05", cap=15,
        )
        assert all(c["departure_date"] <= "2026-05-05" for c in plan.combos)

    def test_errors_when_every_pair_is_impossible(self):
        with pytest.raises(PlanError, match="before every"):
            plan_roundtrip(
                from_airport="TLV", to_airport="FCO",
                departure_date_from="2026-05-10", departure_date_to="2026-05-20",
                return_date="2026-05-01", cap=15,
            )


class TestExecutePlan:
    def test_merges_results_and_counts_calls(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date_from="2026-10-01", departure_date_to="2026-10-05",
            cap=15,
        )

        async def fake_search(endpoint, payload):
            return [{"buy_link": f"link-{payload['departure_date']}"}]

        outcome = asyncio.run(
            execute_plan(plan, lambda c: dict(c), fake_search, max_concurrency=5)
        )
        assert outcome.backend_calls_made == 5
        assert len(outcome.results) == 5
        assert outcome.backend_failures == 0

    def test_one_failure_does_not_sink_the_search(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date_from="2026-10-01", departure_date_to="2026-10-05",
            cap=15,
        )

        async def flaky(endpoint, payload):
            if payload["departure_date"] == "2026-10-03":
                raise RuntimeError("upstream 502")
            return [{"buy_link": payload["departure_date"]}]

        outcome = asyncio.run(
            execute_plan(plan, lambda c: dict(c), flaky, max_concurrency=5)
        )
        assert outcome.backend_failures == 1
        assert len(outcome.results) == 4
        assert "upstream 502" in outcome.first_error

    def test_concurrency_is_bounded(self):
        plan = plan_oneway(
            from_airport="TLV", to_airport="CMB",
            departure_date_from="2026-10-01", departure_date_to="2026-10-15",
            cap=15,
        )
        live = 0
        peak = 0

        async def watched(endpoint, payload):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return []

        asyncio.run(execute_plan(plan, lambda c: dict(c), watched, max_concurrency=3))
        assert peak <= 3, f"semaphore breached: {peak} concurrent calls"


class TestTruncationWording:
    """The note is user-facing copy, and it was inherited from the free
    server's planner where the cap really is a free-tier limit. Here the cap
    is a spend ceiling on the caller's own plan and they can raise it, so the
    free-tier wording would be both wrong and confusing to someone paying."""

    def _truncated_plan(self):
        return plan_oneway(
            from_airport="TLV",
            to_airport="CMB",
            departure_date_from="2026-10-01",
            departure_date_to="2026-10-31",
            cap=30,
        )

    def test_does_not_mention_a_free_tier(self):
        note = self._truncated_plan().coverage()["note"]
        assert "free tier" not in note.lower()

    def test_says_the_searches_are_billed(self):
        note = self._truncated_plan().coverage()["note"]
        assert "billed to your plan" in note

    def test_points_at_the_knob_that_raises_the_cap(self):
        note = self._truncated_plan().coverage()["note"]
        assert "max_searches" in note

    def test_no_note_when_nothing_was_truncated(self):
        plan = plan_oneway(
            from_airport="TLV",
            to_airport="CMB",
            departure_date="2026-10-01",
            cap=30,
        )
        assert "note" not in plan.coverage()
