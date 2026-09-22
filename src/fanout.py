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

Hotels use the same machinery. `POST /search` takes exactly one stay -- one
check-in date, one check-out date -- so "cheapest three nights in Rome in
May" was 31 tool calls or, more often, one arbitrary date and an answer
presented as the cheapest. `plan_hotel_stays` expands a check-in range and a
`nights` value into stays under the same cap, samples the same way when it
does not fit, and reports the same coverage. The wording differs where the
axes differ (a stay has no destination list) and nothing else does.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from .pacing import TokenBucket

MAX_RANGE_DAYS = 180

#: `SearchPlan.endpoint` for a hotel fan-out. Not a backend route name -- the
#: hotel client picks the route -- but the discriminator `coverage()` reads to
#: decide whether it is describing dates and destinations or stays.
STAY_ENDPOINT = "hotel_stays"

#: `FanoutResult.stopped_reason` when the wall clock, not the caller's
#: quota, is what ended the fan-out. See `execute_plan`.
DEADLINE_REACHED = "deadline"


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


#: Delimiters a caller may put between codes inside one string. Whitespace is
#: deliberately not in here -- see `_codes_in`.
_CODE_DELIMITERS = re.compile(r"[,;|/]+")
#: What an IATA airport or city code looks like. Used only to decide whether a
#: space-separated string is a list of codes; real validation lives with the
#: client that talks to the backend.
_LOOKS_LIKE_CODE = re.compile(r"[A-Za-z]{3}")


def _codes_in(text: str) -> list[str]:
    """Split one already-delimited chunk on whitespace, when that is safe.

    "BCN LIS ATH" is three codes. "Tel Aviv" is one (bad) value, and splitting
    it would make the error name `AVIV` instead of what the caller actually
    wrote -- so whitespace separates only when every piece already looks like
    a code.
    """
    text = text.strip()
    if not text:
        return []
    pieces = text.split()
    if len(pieces) > 1 and all(_LOOKS_LIKE_CODE.fullmatch(p) for p in pieces):
        return pieces
    return [text]


def split_airport_codes(value: Any) -> list[str]:
    """Every airport code in one argument, in caller order, case preserved.

    Models do not agree on the shape of a multi-airport argument. One host
    sends `["BCN","LIS","ATH"]`, another sends `"BCN,LIS,ATH"`, a third sends
    `"BCN LIS ATH"` -- all three mean the same search, and until 2026-09-06
    the free server took the string and the paid server took only the list, so
    an LLM that had learned one of them failed against the other. All shapes
    are accepted here, including a list whose own elements are separated
    strings.

    Returns `[]` for None or a blank value, and validates nothing: a bad code
    is still a bad code and is reported by whoever checks codes.
    """
    if value is None:
        return []
    items = list(value) if isinstance(value, (list, tuple, set)) else [value]
    codes: list[str] = []
    for item in items:
        if item is None:
            continue
        for chunk in _CODE_DELIMITERS.split(str(item)):
            codes.extend(_codes_in(chunk))
    return codes


def normalise_airport_codes(value: Any) -> list[str]:
    """`split_airport_codes`, upper-cased, duplicates dropped, order kept."""
    seen: set[str] = set()
    unique: list[str] = []
    for code in split_airport_codes(value):
        code = code.upper()
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


def normalise_destinations(to_airport: str | list[str]) -> list[str]:
    codes = normalise_airport_codes(to_airport)
    if not codes:
        raise PlanError("at least one destination airport is required")
    return codes


def normalise_origin(from_airport: str | list[str]) -> str:
    """The one origin code, accepting the same shapes as a destination list.

    The backend takes a single origin per call and the fan-out is planned over
    dates and destinations only, so several origins cannot be honoured -- say
    so. Before this, `"TLV,JFK"` was upper-cased and sent whole; the upstream
    answered `200 []`, which reads as "no flights on this route".
    """
    codes = normalise_airport_codes(from_airport)
    if not codes:
        raise PlanError("an origin airport is required")
    if len(codes) > 1:
        raise PlanError(
            "one origin airport per search, got "
            + ", ".join(codes)
            + " -- make one call per origin"
        )
    return codes[0]


def _ordered_unique(values) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


@dataclass
class SearchPlan:
    """A capped, ordered list of concrete backend searches."""

    endpoint: Literal["oneway", "roundtrip", "hotel_stays"]
    combos: list[dict[str, str]]
    requested_combinations: int
    cap: int
    degraded_reason: str | None = None
    #: Every combination the request expanded to, BEFORE the cap sampled it.
    #: `combos` is what will actually be searched; this is what was asked for.
    #:
    #: The count alone (`requested_combinations`) was enough while the only
    #: question was "how much did we drop". It is not enough to answer "did
    #: destination X get looked at", because a destination the even sampling
    #: skipped entirely is absent from `combos` and there is then nothing left
    #: in the plan that remembers it was ever requested. The response builds
    #: one entry per requested destination and date off this list, so a
    #: destination that was never searched is a visible hole rather than a
    #: silent omission.
    #:
    #: Defaults to empty for plans built by hand (tests, older callers); the
    #: two accessors below fall back to `combos` in that case.
    requested_combos: list[dict[str, str]] = field(default_factory=list)
    #: WHY the cap is the number it is. Reported in `coverage()` because the
    #: cap moved from "one constant" to "a decision": a caller who sees 93
    #: searches billed for a question they thought cost 30 is owed the
    #: sentence that explains it, and a caller who was sampled down is owed
    #: the difference between "you hit the default" and "you asked for this".
    #: One of `default`, `auto_span`, `explicit`, `trial`.
    cap_source: str = "default"
    #: Set when something other than the cap cut the plan short -- today
    #: only `DEADLINE_REACHED`. It changes the WORDING of the truncation
    #: note, which otherwise tells a caller their range was too wide for the
    #: cap when the cap had nothing to do with it.
    stopped_early: str | None = None

    @property
    def executed_combinations(self) -> int:
        return len(self.combos)

    def narrowed_to(self, combos: list[dict[str, str]], reason: str) -> "SearchPlan":
        """The same request, recorded as having run only `combos`.

        Not a re-sample: the fan-out already decided which combinations went
        out, and this records that. Everything derived from `combos` --
        `executed_combinations`, `truncated`, the dates and destinations the
        coverage lists, the per-destination `not_searched` entries -- then
        describes what actually happened rather than what was planned.
        """
        return SearchPlan(
            endpoint=self.endpoint,
            combos=list(combos),
            requested_combinations=self.requested_combinations,
            cap=self.cap,
            degraded_reason=self.degraded_reason,
            requested_combos=list(self.requested_combos or self.combos),
            cap_source=self.cap_source,
            stopped_early=reason,
        )

    def recap(self, cap: int, *, cap_source: str | None = None) -> "SearchPlan":
        """The same request, sampled against a different cap.

        The cap is not knowable until the plan is: it can depend on how many
        combinations the request expanded to (see AUTO_MAX_SEARCHES). Rather
        than expand the request twice -- once to count it, once to sample it,
        with two chances to disagree -- the planner runs once against the
        highest cap this caller could possibly get and the answer is re-sampled
        here. `requested_combos` is the full, unsampled expansion, so this is
        exactly the plan the planner would have produced with `cap` in hand.
        """
        return _cap_plan(
            self.endpoint,
            list(self.requested_combos or self.combos),
            cap,
            cap_source=cap_source or self.cap_source,
            degraded_reason=self.degraded_reason,
        )

    @property
    def requested_destinations(self) -> list[str]:
        """Destination codes as the caller gave them, in request order."""
        return _ordered_unique(
            str(c.get("to_airport") or "")
            for c in (self.requested_combos or self.combos)
        )

    @property
    def requested_departure_dates(self) -> list[str]:
        """Departure dates as the caller gave them, in request order."""
        return _ordered_unique(
            str(c.get("departure_date") or "")
            for c in (self.requested_combos or self.combos)
        )

    @property
    def requested_checkin_dates(self) -> list[str]:
        """Check-in dates as the caller gave them, in request order."""
        return _ordered_unique(
            str(c.get("checkin_date") or "")
            for c in (self.requested_combos or self.combos)
        )

    @property
    def requested_stays(self) -> list[dict[str, str]]:
        """Every stay the request expanded to, before the cap sampled it."""
        return [
            {"checkin_date": c["checkin_date"], "checkout_date": c["checkout_date"]}
            for c in (self.requested_combos or self.combos)
        ]

    @property
    def truncated(self) -> bool:
        return self.executed_combinations < self.requested_combinations

    def coverage(self) -> dict[str, Any]:
        """Machine- and model-readable description of what was actually searched.

        Always present in the tool result, truncated or not, so the model can
        state honestly what the answer is based on.
        """
        if self.endpoint == STAY_ENDPOINT:
            return self._stay_coverage()

        summary: dict[str, Any] = {
            "requested_combinations": self.requested_combinations,
            "searched_combinations": self.executed_combinations,
            "truncated": self.truncated,
            "max_searches_per_request": self.cap,
            "max_searches_source": self.cap_source,
            "departure_dates_searched": sorted(
                {c["departure_date"] for c in self.combos}
            ),
            "destinations_searched": sorted({c["to_airport"] for c in self.combos}),
        }
        if self.truncated and self.stopped_early == DEADLINE_REACHED:
            summary["stopped_early"] = DEADLINE_REACHED
            summary["note"] = (
                f"This request expanded to {self.requested_combinations} "
                f"searches and {self.executed_combinations} of them ran "
                "before this call reached its time limit; the rest were "
                "never sent and nothing was billed for them. The fares below "
                "are real and paid for, but they do not cover the whole "
                "range. Ask again for the dates that are missing, or narrow "
                "the range so the whole of it fits in one call."
            )
        elif self.truncated:
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

    def _stay_coverage(self) -> dict[str, Any]:
        """`coverage()` for a hotel fan-out.

        Same five facts, and `stays_searched` in place of the two flight axes:
        a stay is a PAIR of dates, so a list of check-in dates alone cannot
        say which lengths were priced. Both are reported -- the pairs for
        exactness, the check-in dates because that is the axis a model reasons
        about when it says "the cheapest date".
        """
        summary: dict[str, Any] = {
            "requested_combinations": self.requested_combinations,
            "searched_combinations": self.executed_combinations,
            "truncated": self.truncated,
            "max_searches_per_request": self.cap,
            "max_searches_source": self.cap_source,
            "stays_searched": [
                {
                    "checkin_date": c["checkin_date"],
                    "checkout_date": c["checkout_date"],
                }
                for c in self.combos
            ],
            "checkin_dates_searched": sorted({c["checkin_date"] for c in self.combos}),
        }
        if self.truncated and self.stopped_early == DEADLINE_REACHED:
            summary["stopped_early"] = DEADLINE_REACHED
            summary["note"] = (
                f"This request expanded to {self.requested_combinations} "
                f"stays and {self.executed_combinations} of them were priced "
                "before this call reached its time limit; the rest were "
                "never sent and nothing was billed for them. Ask again for "
                "the check-in dates that are missing, or narrow the range."
            )
        elif self.truncated:
            summary["note"] = (
                f"This request expanded to {self.requested_combinations} stays, "
                f"above the {self.cap}-search cap for a single call. "
                f"{self.executed_combinations} stays were priced, spread evenly "
                "across the requested check-in range rather than taken from the "
                "start, so the sample is representative but not exhaustive. Each "
                "stay is one request billed to your plan. Narrow the check-in "
                "range or the nights list, or raise max_searches, for fuller "
                "coverage."
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
    # Raised here, not ignored: both planners took an origin they never
    # used, so a multi-origin or blank string reached the payload builder
    # untouched.
    normalise_origin(from_airport)
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
    # Raised here, not ignored: both planners took an origin they never
    # used, so a multi-origin or blank string reached the payload builder
    # untouched.
    normalise_origin(from_airport)
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


def plan_hotel_stays(
    *,
    checkin_date: str | None = None,
    checkout_date: str | None = None,
    checkin_date_from: str | None = None,
    checkin_date_to: str | None = None,
    nights: int | list[int] | None = None,
    cap: int,
) -> SearchPlan:
    """The stays one hotel question expands to, capped and evenly sampled.

    `POST /search` prices exactly one stay, so "cheapest three nights in Rome
    in May" is 31 backend calls. Asking a model to expand that itself has the
    two failure modes the flights planner was written to remove: it fires 31
    unbudgeted requests, or -- far more common in practice -- it picks one
    date, prices it, and reports the number as the cheapest.

    Two forms, and they are alternatives rather than additions:

    * `checkin_date` + `checkout_date` -- one stay, exactly as before.
    * a check-in range (`checkin_date_from`/`checkin_date_to`) and/or `nights`
      (a number, or a list like [2, 3, 7]) -- one stay per check-in date per
      night count, with the check-out date derived.

    A fixed `checkout_date` against a range of check-in dates is allowed and
    means "get out on the 10th, whenever I arrive": the pairs where check-out
    is not after check-in are skipped rather than sent, because the upstream
    computes nights from the two dates and a zero or negative stay fails deep
    rather than as a clean rejection.
    """
    checkins = _resolve_dates(
        checkin_date,
        checkin_date_from,
        checkin_date_to,
        single_name="checkin_date",
        from_name="checkin_date_from",
        to_name="checkin_date_to",
        label="check-in date",
    )

    if checkout_date is not None and nights is not None:
        raise PlanError(
            "give either checkout_date or nights, not both -- nights derives "
            "the check-out date from each check-in date"
        )
    if checkout_date is None and nights is None:
        raise PlanError(
            "a stay needs either checkout_date, or nights (how many nights to "
            "stay) to pair with each check-in date"
        )

    combos: list[dict[str, str]] = []
    if checkout_date is not None:
        leave = parse_iso_date(checkout_date, "checkout_date")
        for day in checkins:
            if leave <= parse_iso_date(day, "checkin_date"):
                continue
            combos.append(
                {"checkin_date": day, "checkout_date": checkout_date.strip()}
            )
        if not combos:
            raise PlanError(
                f"checkout_date {checkout_date} is not after any requested "
                "check-in date"
            )
    else:
        night_options = _normalise_stay_nights(nights)
        # Date-major, so that when the cap samples the list evenly it spreads
        # across the calendar rather than across trip lengths on the same day.
        for day in checkins:
            arrive = parse_iso_date(day, "checkin_date")
            for count in night_options:
                combos.append(
                    {
                        "checkin_date": day,
                        "checkout_date": (
                            arrive + timedelta(days=count)
                        ).isoformat(),
                    }
                )

    return _cap_plan(STAY_ENDPOINT, combos, cap)


def _normalise_stay_nights(nights: int | list[int] | None) -> list[int]:
    """`_normalise_nights`, minus zero.

    A same-day return is a real flight and a zero-night stay is not a stay:
    the upstream derives nights from the two dates, so 0 asks it to price a
    check-in and check-out on the same morning.
    """
    options = [n for n in _normalise_nights(nights) if n >= 1]
    if not options:
        raise PlanError(
            "nights must include at least one value of 1 or more -- a stay is "
            "at least one night"
        )
    return options


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


def _resolve_dates(
    single: str | None,
    start: str | None,
    end: str | None,
    *,
    single_name: str,
    from_name: str,
    to_name: str,
    label: str,
) -> list[str]:
    """One day or an expanded inclusive range, with the caller's own words.

    Parameterised rather than copied for hotels: the four failure modes here
    (both forms at once, half a range, no date at all, an unparseable date)
    are the same four on either axis, and the flights messages are asserted
    verbatim by tests -- so the names travel as arguments and the sentences
    stay in one place.
    """
    if single and (start or end):
        raise PlanError(
            f"give either {single_name} (one day) or "
            f"{from_name}/{to_name} (a range), not both"
        )
    if single:
        parse_iso_date(single, single_name)
        return [single.strip()]
    if start and end:
        return expand_date_range(start, end, label)
    if start or end:
        raise PlanError(f"a {label} range needs both {from_name} and {to_name}")
    raise PlanError(
        f"a {label} is required -- either {single_name}, or "
        f"{from_name} plus {to_name}"
    )


def _resolve_departure_dates(
    departure_date: str | None,
    departure_date_from: str | None,
    departure_date_to: str | None,
) -> list[str]:
    return _resolve_dates(
        departure_date,
        departure_date_from,
        departure_date_to,
        single_name="departure_date",
        from_name="departure_date_from",
        to_name="departure_date_to",
        label="departure date",
    )


def _cap_plan(
    endpoint: Literal["oneway", "roundtrip", "hotel_stays"],
    combos: list[dict[str, str]],
    cap: int,
    *,
    cap_source: str = "default",
    degraded_reason: str | None = None,
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
        requested_combos=list(combos),
        cap_source=cap_source,
        degraded_reason=degraded_reason,
    )


@dataclass
class FanoutResult:
    results: list[dict[str, Any]]
    backend_calls_made: int
    backend_failures: int
    first_error: str | None = None
    #: The combos that raised, in plan order. `backend_failures` counts them;
    #: this says *which*, so the coverage line in the text block can name the
    #: dates a caller asked for and did not get instead of only counting them.
    #: Kept as the combo dicts the plan was built from -- no reformatting here,
    #: because the wording belongs to src/status_text.py, not to the fan-out.
    failed_combos: list[dict[str, str]] = field(default_factory=list)
    #: The rows each answering combination returned, in plan order, as
    #: `(combo, rows)`. `results` is exactly these rows merged -- and merging
    #: is what throws away the one thing a per-combination guarantee needs:
    #: which search a row came from. Without it, a `limit` applied to the
    #: merged list can drop every row of a destination that was searched,
    #: answered, and is still named in `search_coverage`.
    #:
    #: A parallel field rather than a replacement for `results`: several
    #: callers count or scan the merged list and none of them care about the
    #: grouping. Combinations that raised are not in here -- they are in
    #: `failed_combos`, which is a different fact about a different failure.
    results_by_combo: list[tuple[dict[str, str], list[dict[str, Any]]]] = field(
        default_factory=list
    )
    #: Set when a `gate` stopped the fan-out after the first combination.
    #: The combinations that never ran are in `skipped_combos`; they are not
    #: failures -- nothing was attempted and nothing was billed for them.
    stopped_reason: str | None = None
    skipped_combos: list[dict[str, str]] = field(default_factory=list)


async def execute_plan(
    plan: SearchPlan,
    build_payload: Callable[[dict[str, str]], dict[str, Any]],
    run_search: Callable[[str, dict[str, Any]], Any],
    max_concurrency: int,
    pacer: "TokenBucket | None" = None,
    gate: Callable[[], str | None] | None = None,
    deadline_seconds: float = 0.0,
) -> FanoutResult:
    """Run every combo in the plan concurrently, bounded two ways.

    `max_concurrency` bounds how many searches are IN FLIGHT. `pacer` bounds
    how many are STARTED per rolling minute, and they are different limits
    protecting different things:

    * in flight is about this process -- sockets out of the shared pool, and
      the file-descriptor ceiling a Vercel instance shares across executions.
    * the pacer is about the caller's plan, and it is shared by every
      fan-out running on the same key rather than owned by this one. See
      src/pacing.py for why that distinction is the whole point.

    `deadline_seconds`, when set, is a wall clock on the whole fan-out. Past
    it nothing further is DISPATCHED; what is already in flight is left to
    finish, because a request that has been sent is already billed and
    cancelling it throws away a fare the caller has paid for. The
    combinations that never went out come back in `skipped_combos` with
    `stopped_reason` DEADLINE, so the caller can narrow the plan to what
    actually ran and report it as truncated. Without it, a 200-combination
    plan against a slow upstream (~18s a search is enough) runs past the
    function's own `maxDuration`, and the caller gets a dead connection
    having been billed for ~180 searches.

    `gate`, when given, runs the FIRST combination on its own and then asks
    the caller whether to continue; a string back stops the fan-out there and
    nothing further is sent. It exists for one question the caller cannot
    answer in advance: how much quota the key has left, which RapidAPI
    reports only in the headers of a request it has already answered. The
    cost of asking is exactly one request, and that request is a real
    combination of the plan, not a throwaway probe -- if the gate says
    continue, its rows are kept and the remaining combinations run as usual.
    When it stops, those rows are still in `results` -- they were paid for,
    and the caller returns them alongside the refusal.
    The serial first request costs one round trip, so it is worth passing
    only for a fan-out large enough that losing it to a wrong guess matters.

    A single failing combo does not fail the whole search -- with a fan-out
    of 15 across a flaky upstream, all-or-nothing would make large searches
    almost always fail. Failures are counted and the first message kept; the
    caller decides whether a partial answer is worth returning.
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    started_at = time.monotonic()
    deadline = started_at + deadline_seconds if deadline_seconds > 0 else None
    timed_out: list[dict[str, str]] = []

    async def run_one(
        combo: dict[str, str],
    ) -> tuple[dict[str, str], list[dict[str, Any]], str | None, bool]:
        """`(combo, rows, error, dispatched)`.

        `dispatched` False means nothing was sent for this combination and
        nothing was billed -- a different fact from a failure, and the one
        the coverage has to report.
        """
        if pacer is not None:
            # Waited BEFORE the semaphore: the point is to spread the STARTS,
            # and a task holding a slot while it waits its turn spreads
            # nothing. Re-checked in a loop because several waiters wake to
            # the same refilled token and only one of them gets it.
            wait = pacer.take()
            while wait > 0:
                if deadline is not None and time.monotonic() + wait > deadline:
                    # Waiting for a token past the deadline would burn the
                    # whole budget queueing and dispatch nothing.
                    return combo, [], None, False
                await asyncio.sleep(wait)
                wait = pacer.take()
        if deadline is not None and time.monotonic() >= deadline:
            return combo, [], None, False
        async with semaphore:
            # Checked again inside the slot: a combination can queue behind
            # eleven slow searches and reach the front after the clock ran
            # out, and sending it then is money spent on an answer the
            # caller will never see.
            if deadline is not None and time.monotonic() >= deadline:
                return combo, [], None, False
            try:
                rows = await run_search(plan.endpoint, build_payload(combo))
                return combo, rows, None, True
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                return combo, [], f"{combo}: {exc}", True

    combos = plan.combos
    stopped_reason: str | None = None
    skipped: list[dict[str, str]] = []
    raw: list[tuple[dict[str, str], list[dict[str, Any]], str | None, bool]] = []

    if gate is not None and len(combos) > 1:
        raw.append(await run_one(combos[0]))
        stopped_reason = gate()
        if stopped_reason is not None:
            skipped = list(combos[1:])
            combos = combos[:1]

    rest = combos[len(raw):]
    raw += await asyncio.gather(*(run_one(c) for c in rest))

    outcomes: list[tuple[dict[str, str], list[dict[str, Any]], str | None]] = []
    for combo, rows, error, dispatched in raw:
        if dispatched:
            outcomes.append((combo, rows, error))
        else:
            timed_out.append(combo)

    if timed_out:
        # The deadline never overrides a refusal: a fan-out the gate already
        # stopped has a better reason to report than "it took too long".
        skipped = skipped + timed_out
        if stopped_reason is None:
            stopped_reason = DEADLINE_REACHED

    merged: list[dict[str, Any]] = []
    failures = 0
    first_error: str | None = None
    failed_combos: list[dict[str, str]] = []
    results_by_combo: list[tuple[dict[str, str], list[dict[str, Any]]]] = []
    for combo, rows, error in outcomes:
        if error is not None:
            failures += 1
            failed_combos.append(combo)
            if first_error is None:
                first_error = error
            continue
        merged.extend(rows)
        # Kept even when `rows` is empty: "this combination was searched and
        # found nothing" is a different answer from "this combination was
        # never searched", and only the caller can tell them apart.
        results_by_combo.append((combo, list(rows)))

    return FanoutResult(
        results=merged,
        # What was ATTEMPTED, not what was planned: a gated fan-out stops
        # after the first combination and the rest are never sent, so the
        # planned count would bill the caller for requests that do not exist.
        backend_calls_made=len(outcomes),
        backend_failures=failures,
        first_error=first_error,
        failed_combos=failed_combos,
        results_by_combo=results_by_combo,
        stopped_reason=stopped_reason,
        skipped_combos=skipped,
    )
