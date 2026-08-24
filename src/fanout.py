"""
Fan-out planning and execution -- the cost-control core of this server.

Why this module exists
----------------------
The backend takes exactly one (origin, destination, date) tuple per call.
It has no date-range or multi-destination parameter, so a single user intent
like "cheapest flight from TLV to Colombo anywhere in October" is 31 backend
calls. open_claw/SKILL.md:184 tells an LLM to expand those itself and fire
them in parallel.

That is the right behaviour for the *paid* API, where every call is revenue.
It is exactly wrong for the free ad-supported channel, where revenue is per
*rendered ad* -- which is per tool call -- and every backend call is pure
cost. Left alone, one prompt would bill 31 searches against a single ad.

So the tools here expose date ranges and multiple destinations natively and
do the expansion internally, under a hard cap. One user intent becomes one
tool call, one ad, and at most `cap` backend calls. The model no longer
decides how much money we spend.

When a request expands past the cap we sample *evenly across the range*
rather than truncating to the first N. For "cheapest in October", fifteen
dates spread across the month answers the question; the first fifteen days
does not. The reduction is always reported back in `search_coverage` -- a
silently truncated search reads as a complete one, which is how a user ends
up trusting a "cheapest" answer that never looked at the second half of the
month.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Literal

MAX_RANGE_DAYS = 180


class PlanError(ValueError):
    """The requested search could not be turned into a valid plan."""


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise PlanError(
            f"{field_name} must be an ISO date like 2026-10-14, got {value!r}"
        ) from exc


def expand_date_range(start: str, end: str, field_name: str = "departure date") -> list[str]:
    """Inclusive list of ISO dates from start to end."""
    first = parse_iso_date(start, f"{field_name} range start")
    last = parse_iso_date(end, f"{field_name} range end")
    if last < first:
        raise PlanError(
            f"{field_name} range end ({end}) is before its start ({start})"
        )
    span = (last - first).days + 1
    if span > MAX_RANGE_DAYS:
        raise PlanError(
            f"{field_name} range spans {span} days; the maximum is {MAX_RANGE_DAYS}"
        )
    return [(first + timedelta(days=offset)).isoformat() for offset in range(span)]


def evenly_sample(items: list[Any], k: int) -> list[Any]:
    """Pick k items spread evenly across the list, keeping first and last.

    Returns `items` unchanged when it already fits. Order is preserved.
    """
    n = len(items)
    if k >= n:
        return list(items)
    if k <= 0:
        return []
    if k == 1:
        return [items[0]]
    step = (n - 1) / (k - 1)
    picked_indices = sorted({round(i * step) for i in range(k)})
    # Rounding can collide on short lists; backfill so we always return k.
    if len(picked_indices) < k:
        for idx in range(n):
            if len(picked_indices) == k:
                break
            if idx not in picked_indices:
                picked_indices = sorted(picked_indices + [idx])
    return [items[i] for i in picked_indices]


def normalise_destinations(to_airport: str | list[str]) -> list[str]:
    if isinstance(to_airport, str):
        codes = [c.strip().upper() for c in to_airport.split(",")]
    else:
        codes = [str(c).strip().upper() for c in to_airport]
    codes = [c for c in codes if c]
    if not codes:
        raise PlanError("at least one destination airport is required")
    # Preserve caller order, drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


@dataclass
class SearchPlan:
    """A capped, ordered list of concrete backend searches."""

    endpoint: Literal["oneway", "roundtrip"]
    combos: list[dict[str, str]]
    requested_combinations: int
    cap: int
    degraded_reason: str | None = None

    @property
    def executed_combinations(self) -> int:
        return len(self.combos)

    @property
    def truncated(self) -> bool:
        return self.executed_combinations < self.requested_combinations

    def coverage(self) -> dict[str, Any]:
        """Machine- and model-readable description of what was actually searched.

        Always present in the tool result, truncated or not, so the model can
        state honestly what the answer is based on.
        """
        summary: dict[str, Any] = {
            "requested_combinations": self.requested_combinations,
            "searched_combinations": self.executed_combinations,
            "truncated": self.truncated,
            "max_searches_per_request": self.cap,
            "departure_dates_searched": sorted(
                {c["departure_date"] for c in self.combos}
            ),
            "destinations_searched": sorted({c["to_airport"] for c in self.combos}),
        }
        if self.truncated:
            # Wording differs from the free server's planner on purpose: there
            # the cap is a free-tier limit, here it is a spend ceiling on the
            # caller's own plan, and they can raise it. Telling a paying user
            # they hit "the free tier limit" is both wrong and confusing.
            summary["note"] = (
                f"This request expanded to {self.requested_combinations} searches, "
                f"above the {self.cap}-search cap for a single call. "
                f"{self.executed_combinations} searches were run, spread evenly "
                "across the requested range rather than taken from the start, so "
                "the sample is representative but not exhaustive. Each search is "
                "one request billed to your plan. Narrow the date range or "
                "destination list, or raise max_searches, for fuller coverage."
            )
        if self.degraded_reason:
            summary["degraded"] = self.degraded_reason
        return summary


def plan_oneway(
    *,
    from_airport: str,
    to_airport: str | list[str],
    departure_date: str | None = None,
    departure_date_from: str | None = None,
    departure_date_to: str | None = None,
    cap: int,
) -> SearchPlan:
    destinations = normalise_destinations(to_airport)
    dates = _resolve_departure_dates(
        departure_date, departure_date_from, departure_date_to
    )

    combos = [
        {"departure_date": day, "to_airport": dest}
        for day in dates
        for dest in destinations
    ]
    return _cap_plan("oneway", combos, cap)


def plan_roundtrip(
    *,
    from_airport: str,
    to_airport: str | list[str],
    departure_date: str | None = None,
    departure_date_from: str | None = None,
    departure_date_to: str | None = None,
    return_date: str | None = None,
    nights: int | list[int] | None = None,
    cap: int,
) -> SearchPlan:
    destinations = normalise_destinations(to_airport)
    dates = _resolve_departure_dates(
        departure_date, departure_date_from, departure_date_to
    )

    if return_date is None and nights is None:
        raise PlanError(
            "roundtrip needs either return_date, or nights (a trip length) "
            "to pair with each departure date"
        )
    if return_date is not None and nights is not None:
        raise PlanError(
            "give either return_date or nights, not both -- nights derives the "
            "return date from each departure date"
        )

    combos: list[dict[str, str]] = []
    if return_date is not None:
        parse_iso_date(return_date, "return_date")
        for day in dates:
            if parse_iso_date(return_date, "return_date") < parse_iso_date(
                day, "departure_date"
            ):
                # Skip impossible pairs rather than sending them: the backend
                # computes nights from the two dates (api_lambda.py:54) and a
                # negative value fails deep in the stack, not as a clean 422.
                continue
            for dest in destinations:
                combos.append(
                    {
                        "departure_date": day,
                        "return_date": return_date,
                        "to_airport": dest,
                    }
                )
        if not combos:
            raise PlanError(
                f"return_date {return_date} is before every requested departure date"
            )
    else:
        night_options = _normalise_nights(nights)
        for day in dates:
            departure = parse_iso_date(day, "departure_date")
            for count in night_options:
                back = (departure + timedelta(days=count)).isoformat()
                for dest in destinations:
                    combos.append(
                        {
                            "departure_date": day,
                            "return_date": back,
                            "to_airport": dest,
                        }
                    )

    return _cap_plan("roundtrip", combos, cap)


def _normalise_nights(nights: int | list[int] | None) -> list[int]:
    if isinstance(nights, int):
        options = [nights]
    elif isinstance(nights, (list, tuple)):
        options = [int(n) for n in nights]
    else:
        raise PlanError(f"nights must be a number or list of numbers, got {nights!r}")
    options = sorted({n for n in options if n >= 0})
    if not options:
        raise PlanError("nights must include at least one non-negative value")
    if any(n > MAX_RANGE_DAYS for n in options):
        raise PlanError(f"nights values must not exceed {MAX_RANGE_DAYS}")
    return options


def _resolve_departure_dates(
    departure_date: str | None,
    departure_date_from: str | None,
    departure_date_to: str | None,
) -> list[str]:
    if departure_date and (departure_date_from or departure_date_to):
        raise PlanError(
            "give either departure_date (one day) or "
            "departure_date_from/departure_date_to (a range), not both"
        )
    if departure_date:
        parse_iso_date(departure_date, "departure_date")
        return [departure_date.strip()]
    if departure_date_from and departure_date_to:
        return expand_date_range(departure_date_from, departure_date_to)
    if departure_date_from or departure_date_to:
        raise PlanError(
            "a departure date range needs both departure_date_from and "
            "departure_date_to"
        )
    raise PlanError(
        "a departure date is required -- either departure_date, or "
        "departure_date_from plus departure_date_to"
    )


def _cap_plan(
    endpoint: Literal["oneway", "roundtrip"],
    combos: list[dict[str, str]],
    cap: int,
) -> SearchPlan:
    requested = len(combos)
    if requested == 0:
        raise PlanError("the request expanded to zero searches")
    capped = evenly_sample(combos, cap) if requested > cap else combos
    return SearchPlan(
        endpoint=endpoint,
        combos=capped,
        requested_combinations=requested,
        cap=cap,
    )


@dataclass
class FanoutResult:
    results: list[dict[str, Any]]
    backend_calls_made: int
    backend_failures: int
    first_error: str | None = None


async def execute_plan(
    plan: SearchPlan,
    build_payload: Callable[[dict[str, str]], dict[str, Any]],
    run_search: Callable[[str, dict[str, Any]], Any],
    max_concurrency: int,
) -> FanoutResult:
    """Run every combo in the plan concurrently, bounded by max_concurrency.

    A single failing combo does not fail the whole search -- with a fan-out
    of 15 across a flaky upstream, all-or-nothing would make large searches
    almost always fail. Failures are counted and the first message kept; the
    caller decides whether a partial answer is worth returning.
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def run_one(combo: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
        async with semaphore:
            try:
                rows = await run_search(plan.endpoint, build_payload(combo))
                return rows, None
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                return [], f"{combo}: {exc}"

    outcomes = await asyncio.gather(*(run_one(c) for c in plan.combos))

    merged: list[dict[str, Any]] = []
    failures = 0
    first_error: str | None = None
    for rows, error in outcomes:
        if error is not None:
            failures += 1
            if first_error is None:
                first_error = error
            continue
        merged.extend(rows)

    return FanoutResult(
        results=merged,
        backend_calls_made=len(plan.combos),
        backend_failures=failures,
        first_error=first_error,
    )
