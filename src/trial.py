"""
The keyless allowance: what a signed-in caller with no RapidAPI key gets.

Why this exists
---------------
Every search on this server is billed to the caller's own RapidAPI
subscription, and until now a caller who signed in with Google but had never
pasted a key got a refusal -- a correct one, and a dead end. Two groups walk
into that dead end:

* **Reviewers.** OpenAI rejected the ChatGPT app on 2026-09-15 because five of
  five test cases came back 401. The submission form then demands working test
  credentials, and the only ones we could give were somebody's real RapidAPI
  key (the form forbids a real account) or a paid key bought for a reviewer
  (rule 15 forbids the spend). A reviewer who can click "Sign in with Google"
  and get a real fare needs neither.
* **Everybody's first five minutes.** A directory listing sends a stranger to
  a server that answers their first question with homework. The RapidAPI free
  BASIC tier is ten requests a MONTH, so "go get a free key" is not a fix
  either -- a single flexible search spends more than that.

So: sign in, and the first `PAID_TRIAL_DAY_CAP` backend searches of each UTC
day run on OUR key. Not a plan, not a trial period with an end date -- a small
daily allowance that renews, exactly like the free server's, minus the ads.

What is counted
---------------
BACKEND searches, not tool calls -- the same unit the free server counts and
for the same reason (`mcp_server/src/fair_use.py`): one tool call with a
two-week date range and three destinations is 42 backend calls, and counting
tool calls would leave the expensive shape uncapped. A date x destination
combination is one search.

Per Google `sub`, per UTC day, in Postgres (`paid_trial_usage`). Never a
global counter: one busy caller must not be able to spend the allowance every
other caller was promised, which is the exact failure the free server's
DAILY_BACKEND_CALL_BUDGET has and the reason it needed a per-client cap on
top.

What it is NOT
--------------
* Not ads. This server carries no sponsored content and cannot (rule 5:
  Anthropic Directory Policy 4.C and the OpenAI app guidelines both ban it),
  so the allowance is the only free thing here.
* Not a way around the paid product. Ten searches a day is a taste; the
  message on every result says what removes the cap, and the cap message says
  it again. RapidAPI PRO is $10 for 2,500 requests -- roughly eight times the
  allowance's monthly total -- so the allowance can never be the better deal.
* Not a refusal. Over the cap the tools answer with a normal result envelope
  and `search_status: "trial_exhausted"`, never a 401 and never an exception:
  a model handed an HTTP error retries it, and there is nothing to retry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

#: `Credential.source` for a request served from the allowance. A distinct
#: value, not an empty key and not `env:RAPIDAPI_KEY`: the call sites branch on
#: it (spend the allowance, tag the backend call, attach the note), the
#: telemetry line records it, and nothing else in the file must have to guess
#: from a key value whose bill this is.
TRIAL_SOURCE = "trial:allowance"

#: `search_status` on a result the allowance refused.
TRIAL_EXHAUSTED = "trial_exhausted"

#: The default is ZERO: no allowance. This feature spends OUR key, and a
#: default that spends money is a default that gets switched on by a deploy
#: nobody read -- including on a preview, where nobody would ever look at the
#: bill. It exists only where `PAID_TRIAL_DAY_CAP` and
#: `PAID_TRIAL_RAPIDAPI_KEY` are BOTH set on purpose, and there is deliberately
#: no fallback to `RAPIDAPI_KEY`: that variable already has a meaning (serve
#: every keyless caller on the deployment owner's plan) and inheriting it would
#: turn one dangerous switch into two.
DEFAULT_DAY_CAP = 0

#: What to set `PAID_TRIAL_DAY_CAP` to. Ten is deliberately small: it covers a
#: reviewer's five test cases twice over, and it is one twenty-fifth of
#: RapidAPI PRO's monthly quota, so the free thing can never undercut the paid
#: one. Quoted in example.env and the README; never applied by default.
SUGGESTED_DAY_CAP = 10

#: What the Lambda's `[source]` line and the daily rollup call this traffic.
#: One value for both products (rule 11): every backend caller is attributable,
#: and "paid-trial" is the only surface that spends OUR key on the paid server.
FP_SOURCE = "paid-trial"

#: ...and what the same lines call a call on the CALLER'S OWN key. Until
#: 2026-09-17 a keyed request from this server carried no attribution at all,
#: so it reached the backend as an untagged Hub request and was logged as
#: `source=rapidapi` -- the bucket rule 11 reserves for direct Hub calls. Two
#: consequences, and the second one is why this is a bug rather than a gap:
#:
#: 1. the paid MCP server was uncountable. Every keyed search it has ever sent
#:    is sitting in the same bucket as a customer curling the Hub, so "how much
#:    traffic does the MCP server carry" had no answer;
#: 2. the only paid-server traffic that named itself was the allowance's. Its
#:    calls spend OUR key, so the Hub stamps them `x-rapidapi-user: mtnrabi` --
#:    the owner of `PAID_TRIAL_RAPIDAPI_KEY`, not the caller (the flights
#:    backend takes no `_fp_user`, see below). A `[lane]` line reading
#:    `source=paid-trial user=mtnrabi` is therefore exactly what a *correct*
#:    allowance call looks like, and with keyed calls invisible there was
#:    nothing on the line to tell it apart from a keyed one that had been
#:    mis-tagged.
#:
#: The tags never decide anything -- they are stripped before validation and
#: read only by the log -- so this cannot change what a caller is served.
KEYED_SOURCE = "paid-mcp"

#: The two attribution fields the FLIGHTS Lambda takes out of the request body
#: before validation (`backend/src/api_lambda.py`: SOURCE_FIELD / TOOL_FIELD).
#: Headers do not survive the RapidAPI Hub and this path goes through it, so
#: the body is the only channel.
SOURCE_FIELD = "_fp_source"
TOOL_FIELD = "_fp_tool"
#: Who, as well as what. The hotels backend reads it (X-FP-User -> _fp_user ->
#: x-rapidapi-user, live on both hotel functions since 2026-09-13). The flights
#: Lambda does NOT: `AttributionRoute` strips only the two fields above, and
#: `OneWayAPI`/`RoundtripAPI` are `extra="forbid"`, so sending it on a flights
#: search is a 422 -- an invisible one, since a validation reject prints
#: nothing in CloudWatch. Hence `body_tags` is per product, and the flights
#: half of "who" lives in `paid_trial_usage` and the `[paid_trial]` log line
#: until the backend learns the field.
USER_FIELD = "_fp_user"

#: Products whose backend accepts `_fp_user` in the body. See above.
_USER_FIELD_PRODUCTS = ("hotels",)


def utc_day(now: datetime | None = None) -> date:
    """The allowance day. UTC, always, so a caller cannot get two allowances
    by moving timezone and so the rollover is the same instant everywhere."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def body_tags(product: str, tool: str, user: str = "",
              source: str = FP_SOURCE) -> dict[str, str]:
    """Attribution to merge into a backend request body.

    `source` is the surface: FP_SOURCE on the allowance path, KEYED_SOURCE on
    a call billed to the caller's own key. It is a parameter rather than two
    copies of this function because the *fields* are the delicate part -- which
    product may be sent a `_fp_user`, what an empty value does to the rollup --
    and those are identical on both paths.

    `user` is only emitted for a product whose backend can take it; see
    USER_FIELD. Empty strings are dropped rather than sent, because a blank
    `_fp_source` would be logged as a source named "" and count as its own
    surface in the rollup.
    """
    tags = {SOURCE_FIELD: source or FP_SOURCE}
    if tool:
        tags[TOOL_FIELD] = tool
    if user and product in _USER_FIELD_PRODUCTS:
        tags[USER_FIELD] = user
    return tags


def keyed_body_tags(product: str, tool: str) -> dict[str, str]:
    """Attribution for a search billed to the caller's own RapidAPI key.

    Never a `_fp_user`, on any product. On the allowance path `_fp_user` is the
    only way to say who spent our key; here the Hub has already named the caller
    truthfully -- `x-rapidapi-user` is the owner of the key that is being billed
    -- and `_fp_user` *outranks* it in the backend's resolution order
    (X-FP-User -> _fp_user -> x-rapidapi-user). Sending one would replace a
    paying customer's RapidAPI username with an opaque Google subject id in
    every log line they appear in, which is the identity rule 11 exists to keep.
    """
    return body_tags(product, tool, source=KEYED_SOURCE)


@dataclass(frozen=True)
class TrialState:
    """One signed-in account's standing against the allowance, right now.

    Read before the searches run and rebuilt with `after()` once they have, so
    the number a caller is shown includes the call being answered rather than
    the one before it -- same contract as the free server's FairUseState.
    """

    user_sub: str
    email: str
    used_today: int
    day_cap: int

    @property
    def remaining(self) -> int:
        if self.day_cap <= 0:
            return 0
        return max(0, self.day_cap - self.used_today)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def after(self, spent: int) -> "TrialState":
        return replace(self, used_today=self.used_today + max(0, spent))


def _who(state: TrialState) -> str:
    return f" on {state.email}" if state.email else ""


def trial_note(state: TrialState, connect_url: str, signup_url: str) -> dict[str, Any]:
    """The `trial` block carried on EVERY result served from the allowance.

    Small and boring on purpose: the two counters, and one sentence a model can
    read out loud. A caller who only learns the number when it runs out has no
    chance to do anything about it, which is the mistake the free server fixed
    on 2026-09-09 by showing usage from the first call rather than from 80%.
    """
    return {
        "used_today": state.used_today,
        "day_cap": state.day_cap,
        "remaining_today": state.remaining,
        "key_connected": False,
        "user": state.email or None,
        "human": (
            f"{state.used_today:,} of {state.day_cap:,} free searches used "
            f"today{_who(state)}. The allowance renews at 00:00 UTC."
        ),
        "note": (
            "These searches ran on FlightPowers' own RapidAPI key, not yours. "
            f"Connect your own key once at {connect_url} and the cap goes "
            f"away -- a free RapidAPI key from {signup_url} is enough to "
            "start, and usage is then billed to your own plan."
        ),
    }


#: `reason` on `trial_exhausted_result`. Two different true sentences.
#: `SPENT` is the ordinary case: this account has used its allowance.
#: `UNAVAILABLE` is the reservation losing a race with the account's own other
#: call, or the counter being unreachable -- the allowance is not necessarily
#: gone, it just could not be held for THIS search. Saying "it is spent" there
#: would be a number the caller could check and find wrong.
REASON_SPENT = "spent"
REASON_UNAVAILABLE = "unavailable"


def trial_exhausted_result(
    state: TrialState,
    connect_url: str,
    signup_url: str,
    api_name: str,
    reason: str = REASON_SPENT,
) -> dict[str, Any]:
    """The whole tool result for a call the allowance refused.

    The same shape a search returns, not a ToolError and not a 401: a model
    handed an exception retries it, and a retry cannot succeed until tomorrow.
    `retry: false` says so in the payload and `message` says so in the text
    block every client shows, because a script sees only the text.
    """
    if reason == REASON_UNAVAILABLE:
        opening = (
            "This search was not run: the free allowance for this account "
            f"could not cover it right now ({state.used_today:,}/"
            f"{state.day_cap:,} today{_who(state)}), because another search on "
            "the same account is using it or the counter could not be read. "
            "The allowance renews at 00:00 UTC."
        )
    else:
        opening = (
            "This search was not run: the free allowance for this account is "
            f"spent ({state.used_today:,}/{state.day_cap:,} today{_who(state)}) "
            "and it renews at 00:00 UTC. Retrying will not help."
        )
    return {
        "results": [],
        "result_count": 0,
        "search_status": TRIAL_EXHAUSTED,
        "retry": False,
        "message": (
            f"{opening} Connect your own RapidAPI key once at {connect_url} "
            "-- nothing to change in this client, searches start working "
            "immediately and are billed to your own plan. A free key for the "
            f"{api_name} is at {signup_url}. Tell the user this rather than "
            "reporting a plain failure."
        ),
        "trial": {
            "used_today": state.used_today,
            "day_cap": state.day_cap,
            "remaining_today": state.remaining,
            "key_connected": False,
            "user": state.email or None,
            "exhausted": True,
            "reason": reason,
        },
        "how_to_get_a_key": {
            "connect_url": connect_url,
            "signup_url": signup_url,
            "how": [
                f"1. Get a RapidAPI key (free tier available): subscribe to "
                f"the {api_name} at {signup_url}.",
                f"2. Open {connect_url}, sign in with the same Google account "
                "you signed in with here, and paste the key once. Nothing else "
                "to change: this connection starts working immediately.",
                "3. Usage then counts against your own RapidAPI plan, not "
                "ours, and every response reports what the call spent and what "
                "is left, in `api_usage`.",
            ],
        },
    }


def trial_unavailable_reason() -> str:
    """Why a signed-in caller was refused when the STORE, not the cap, failed.

    Deliberately not `trial_exhausted`: the caller has spent nothing, and
    telling them their allowance is gone when the database is down is a lie
    they would act on. They get the ordinary "connect a key" reply instead, and
    this string is what the log line says.
    """
    return "trial_store_unavailable"


def compare_needs_own_key_message(connect_url: str, signup_url: str) -> str:
    """Why comparing several sources is not part of the allowance.

    The allowance runs on one key -- ours -- and a cross-source comparison
    reaches sources that are not on the RapidAPI edge at all (Airbnb goes
    through the api front). Rather than quietly bill those to us, the compare
    path asks for the caller's own key and says why in one sentence.
    """
    return (
        "Comparing several sources in one call needs your own RapidAPI key: "
        "the free signed-in allowance covers single-source searches only. "
        f"Paste a key once at {connect_url} (a free one from {signup_url} is "
        "enough to start) and this call works. Nothing was searched and "
        "nothing was billed."
    )


def trial_tail(day_cap: int, connect_url: str) -> str:
    """One sentence for the server `instructions` and every tool description.

    A model that only learns the allowance exists from inside a refusal has
    already told the user this server needs a key it does not need.
    """
    return (
        f"No key? Sign in with Google and the first {day_cap} searches each "
        f"day are free on this server -- ad-free, nothing to paste. Connect "
        f"your own RapidAPI key at {connect_url} to remove the cap."
    )


def log_line(state: TrialState, action: str, tool: str, spent: int = 0) -> str:
    """The `[paid_trial]` line. The account, the tool, the counters.

    The Google `sub` rather than the email: it is the key the row is stored
    under, it is stable across an address change, and it is not a contact
    detail sitting in a log. Nothing here is a credential -- our own key is
    never named, redacted or otherwise.
    """
    return (
        f"[paid_trial] action={action} tool={tool} sub={state.user_sub} "
        f"spent={spent} used_today={state.used_today} cap={state.day_cap}"
    )
