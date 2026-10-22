"""
Refuse a fan-out the caller's free allowance cannot pay for -- before it runs.

Why this module exists
----------------------
Raising the cap so that "every departure in October at 3, 4 or 5 nights" is
one call (93 requests) made an old, quiet behaviour loud. Someone on a free
allowance -- the keyless Google sign-in, or the Hub's BASIC plan with ten
requests a month -- asks that question, and the server samples it down to
whatever they have left and answers. The answer looks like an answer: it names
a cheapest fare and a date. It is a cheapest fare out of eight of the ninety
three combinations they asked about, and nothing in the reply is the sentence
they actually need, which is "this question costs 93 requests and you have
10".

So a free caller whose allowance cannot cover the request gets a refusal
instead of a sample: no search runs, nothing is spent, and the reply carries
the two numbers and the two ways forward. A caller with enough quota is never
refused -- this is a gate on "cannot pay", not on "asked for a lot".

The two free paths are not symmetrical, and that is a fact about where the
counter lives, not a design choice:

* the **trial** (a signed-in caller with no key of their own, spending OUR
  key) is counted in our own store, so the refusal happens before the first
  request -- nothing at all is spent.
* a **RapidAPI key** has its remaining quota only in RapidAPI's response
  headers (`x-ratelimit-requests-remaining`), which do not exist until a
  request has been answered. So the fan-out spends ONE request, reads the
  headers off it, and stops there. One request is the floor; there is no way
  to learn the number for free, and guessing the plan from the key is not a
  thing the gateway supports.

Deliberately NOT plan-name detection. Whether a key is on BASIC (free) or PRO
is not reliably readable, and it does not need to be: "remaining < requested"
is the condition that matters and it is true of exactly the callers this is
for. A PRO key with 2,000 left asking for 93 never sees this.
"""

from __future__ import annotations

from typing import Any

#: `search_status` for a refusal. New value, not one of the upstream's: this
#: is a decision this server took, not something the backend reported.
QUOTA_EXCEEDED = "quota_exceeded"

#: The listed entry plan, per listing, as sold on the RapidAPI pricing tabs:
#: google-flights-live-api PRO is $10 a month for 2,500 requests,
#: booking-live-api PRO is $10 a month for 2,000. BASIC is $0 for 10 a month
#: on both, which is why it is a footnote and never the lead: one month-wide
#: scan is 93 searches. Written down once, here, rather than in every
#: message string.
PRO_PRICE_USD = 10
PRO_SEARCHES = {"flights": 2500, "hotels": 2000}
BASIC_SEARCHES = 10
#: A whole month of dates at three trip lengths (31 x 3), the same figure the
#: tool descriptions use. No longer quoted in a tool result (2026-10-22: "PRO
#: covers about 26 of those a month" read as a value pitch to a zero-context
#: directory review); kept as the one place the figure is written down.
MONTH_SCAN_SEARCHES = 93


def _plan_product(signup_url: str) -> str:
    """Which listing a signup URL is, so the quota quoted is that listing's."""
    return "hotels" if "booking-live-api" in (signup_url or "") else "flights"


def pricing_url(signup_url: str) -> str:
    """The listing's pricing tab: where the plans and the Subscribe buttons are."""
    return f"{(signup_url or '').rstrip('/')}/pricing"


def pro_line(signup_url: str) -> str:
    """'PRO is $10 a month for 2,500 searches', for this listing."""
    searches = PRO_SEARCHES[_plan_product(signup_url)]
    return f"PRO is ${PRO_PRICE_USD} a month for {searches:,} searches"


BASIC_FOOTNOTE = (
    f"(RapidAPI's BASIC plan is free but has only {BASIC_SEARCHES} searches "
    "a month.)"
)


def _shrink_hint(requested: int, allowed: int) -> str:
    """A concrete smaller question, sized to what they can actually afford.

    A bare "ask for less" is not actionable to a model that has just been
    told a month costs 93. One week at one trip length is 7, which fits
    almost any remainder, so the hint names the axes to cut rather than
    inventing a range we cannot know is useful.
    """
    if allowed <= 0:
        return "fewer dates and fewer trip lengths"
    if allowed < 7:
        return f"a shorter range -- {allowed} date/night combinations or fewer"
    return "fewer dates or nights (e.g. one week, 3 nights)"


def trial_quota_refusal(
    *,
    requested: int,
    remaining_today: int,
    day_cap: int,
    connect_url: str,
    signup_url: str,
) -> dict[str, Any]:
    """The reply for a trial caller who asked for more than today's allowance.

    Same shape as `trial_exhausted_result` -- data, `retry: False`, and the
    whole story in `message` -- because a model handed an exception retries
    it, and a retry cannot succeed until the request is smaller.
    """
    return {
        "results": [],
        "result_count": 0,
        "search_status": QUOTA_EXCEEDED,
        "retry": False,
        "combos_requested": requested,
        "combos_allowed_now": remaining_today,
        "remaining_today": remaining_today,
        "message": (
            f"This search needs {requested:,} searches but your free "
            f"allowance has {remaining_today:,} left today "
            f"({day_cap:,} a day, renewing at 00:00 UTC). Nothing was "
            f"searched and nothing was spent. {pro_line(signup_url)} "
            f"({pricing_url(signup_url)}); subscribe there and connect your "
            f"key once at {connect_url}, and this allowance stops applying. "
            "Or a search of "
            f"{_shrink_hint(requested, remaining_today)} needs fewer of "
            "today's free searches. Retrying the same "
            "search will not help."
        ),
        "trial": {
            "day_cap": day_cap,
            "remaining_today": remaining_today,
            "key_connected": False,
        },
        "signup_url": signup_url,
        "pricing_url": pricing_url(signup_url),
        "connect_url": connect_url,
    }


def plan_quota_refusal(
    *,
    requested: int,
    remaining_month: int,
    spent_probing: int,
    signup_url: str,
    plan_limit: int | None = None,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The reply for a keyed caller whose plan cannot cover the fan-out.

    `spent_probing` is honest and load-bearing: the remaining figure came
    from a real response, so the caller is told the one request it cost.

    And `results` is that request's OWN fares. The first version threw them
    away and returned `results: []`, which billed the caller for a real
    search and handed them nothing -- the exact behaviour the refusal exists
    to prevent, in miniature. The combination that read the quota is a real
    combination of the plan; its rows are one date out of the range they
    asked for, they are paid for, and they belong in the answer next to the
    sentence explaining why the other 92 did not run.
    """
    plan_bit = (
        f" (plan quota {plan_limit:,} a month)"
        if plan_limit is not None and plan_limit > 0
        else ""
    )
    rows = list(results or [])
    kept = (
        ""
        if not rows
        else (
            f" The {len(rows)} fare(s) below came from that one search and "
            "are real, but they cover a single date out of the range you "
            "asked about, so whether they are the cheapest of the whole range "
            "is unknown."
        )
    )
    return {
        "results": rows,
        "result_count": len(rows),
        "search_status": QUOTA_EXCEEDED,
        "retry": False,
        "combos_requested": requested,
        "combos_searched": 1 if rows else 0,
        "combos_allowed_now": remaining_month,
        "remaining_month": remaining_month,
        "requests_spent": spent_probing,
        "message": (
            f"This search needs {requested:,} requests but your plan has "
            f"{remaining_month:,} left this month{plan_bit}. It was stopped "
            f"after {spent_probing} request, which is what it cost to read "
            "the remaining quota, so the rest of the plan's balance is "
            f"untouched.{kept} A search of "
            f"{_shrink_hint(requested, remaining_month)} needs fewer "
            f"requests; a larger plan is at {pricing_url(signup_url)}. "
            "Retrying the same search will not help."
        ),
        "signup_url": signup_url,
    }
