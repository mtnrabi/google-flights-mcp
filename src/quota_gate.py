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

#: The listed entry plan on BOTH listings (google-flights-live-api and
#: booking-live-api sell PRO at $10 for 2,500 requests; BASIC is $0 for 10).
#: Written down once, here, rather than in three message strings.
PRO_PLAN_LINE = "PRO ($10/month, 2,500 requests)"


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
            f"This search needs {requested:,} requests but your free "
            f"allowance has {remaining_today:,} left today "
            f"({day_cap:,} a day, renewing at 00:00 UTC). Nothing was "
            f"searched and nothing was spent. Ask for "
            f"{_shrink_hint(requested, remaining_today)}, or subscribe to "
            f"{PRO_PLAN_LINE} at {signup_url} and connect your key at "
            f"{connect_url} -- searches then bill your own plan and this "
            "allowance stops applying. Tell the user this rather than "
            "reporting a plain failure, and do not retry the same search."
        ),
        "trial": {
            "day_cap": day_cap,
            "remaining_today": remaining_today,
            "key_connected": False,
        },
        "signup_url": signup_url,
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
            "asked about -- say so rather than presenting them as the "
            "cheapest of the whole range."
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
            f"untouched.{kept} Ask for "
            f"{_shrink_hint(requested, remaining_month)}, "
            f"or move to {PRO_PLAN_LINE} or a larger plan at {signup_url}. "
            "Tell the user this rather than reporting a plain failure, and "
            "do not retry the same search."
        ),
        "signup_url": signup_url,
    }
