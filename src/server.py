"""
Google Flights MCP server -- ad-free, caller-funded.

Same search engine as the free server; three deliberate differences.

* **No ads, anywhere.** No sponsored widget, no ad SDK, no beacon, nothing in
  a tool result that is not flight data. This is not a matter of taste: both
  Anthropic's connector directory policy and OpenAI's app guidelines prohibit
  advertising and sponsored content in tool results, so the free server can
  never be listed there and this one can. Do not add an ad to this package.

* **The caller pays.** Every upstream request is billed to the caller's own
  RapidAPI subscription, so the key arrives per request (credentials.py) and
  this process holds no upstream credential of its own.

* **Spend is reported, not hidden.** Because the money is the caller's, every
  response carries `api_usage`: how many billed requests this call consumed
  and how many remain on their plan. The single fastest way to lose a paying
  user is for their quota to vanish into a fan-out they never saw.

Design notes carried over from the free server, still true here
---------------------------------------------------------------
* Fan-out is internal. Both tools take a date range and a destination list, so
  one user intent is one tool call. On a passthrough that exposes one date per
  call, "cheapest to Sri Lanka anywhere in October" is 31 separate billed
  requests plus 31 round trips; here it is one call, capped, evenly sampled,
  and honestly reported.

* `sort_type` is deliberately NOT exposed. On the backend it selects which
  search runs rather than post-sorting, and `max_price` overrides it outright.
  Results from up to `cap` searches are merged here anyway, so a per-search
  ordering would be discarded by the merge; this server lets the backend
  default apply and sorts the merged set itself via `sort_by`. That is
  predictable; passing `sort_type` through is not.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .connect import (
    build_connect_support,
    check_rapidapi_key,
    signed_in_html,
    signed_out_html,
)
from .credentials import (
    NO_CREDENTIAL,
    Credential,
    find_connect_token,
    key_howto_block,
    key_howto_tail,
    key_looks_malformed,
    missing_key_message,
    redact,
    resolve_credential,
)
from .keystore import PROVIDER_GOOGLE, KeyStoreError
from .oauth import (
    MCP_OAUTH_PATH,
    PROVIDER_HEADER,
    SUBJECT_HEADER,
    build_oauth_support,
)
from .oauthroutes import register_oauth_routes
from .oauthstore import OAuthStoreError
from .webauth import (
    COOKIE_PATH,
    OAUTH_COOKIE,
    OAUTH_STATE_TTL_SECONDS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    WebAuthError,
)
from .legal import CONTACT_EMAIL, index_html, support_html, render_document
from .hotels_client import (
    VALID_FILTERS,
    HotelsClient,
    build_hotel_by_name_payload,
    build_search_payload,
    unknown_filters,
)
from .output_schema import (
    FLIGHTS_OUTPUT_SCHEMA,
    HOTELS_OUTPUT_SCHEMA,
)
from .prompts import register_prompts
from .schema_docs import document_params
from .fanout import (
    FanoutResult,
    PlanError,
    SearchPlan,
    execute_plan,
    normalise_origin,
    plan_oneway,
    plan_roundtrip,
)
from .rapidapi_client import (
    SEARCH_REASON_HEADER,
    SEARCH_STATUS_HEADER,
    AuthError,
    QuotaError,
    RapidAPIClient,
    RapidAPIError,
    build_oneway_payload,
    build_roundtrip_payload,
    invalid_airports,
    search_is_incomplete,
)
from .status_text import (
    DEGRADED_FIRST_LINE,
    MAX_NAMED_COMBINATIONS,
    describe_combination,
    partial_first_line,
    serialize_payload,
)
from .settings import (
    HARD_MAX_SEARCHES,
    VALID_PRODUCTS,
    Settings,
    load_settings,
)
from .stores import build_counter_store
from .telemetry import CallRecord, Telemetry

logger = logging.getLogger(__name__)

SORT_CHOICES = ("best", "price", "duration")

SERVICE_NAME = "google-flights-mcp"

# Per-deployment name. `service` is what /health reports and what registries
# poll, so a hotels-only deployment calling itself "google-flights-mcp" is a
# small lie in a place people read.
SERVICE_NAMES = {
    "flights": "google-flights-mcp",
    "hotels": "booking-hotels-mcp",
    "both": "flightpowers-travel-mcp",
}


def service_name(products: str) -> str:
    return SERVICE_NAMES.get(products, SERVICE_NAME)


# The RapidAPI listing whose subscription pays for a search, per product.
#
# One map, read by every string that names it: the server instructions, the
# keyless reply, and the auth-failure reply. It exists because those three
# had drifted -- the instructions were product-aware while the keyless reply
# still told hotels callers their search was "billed to the caller's own
# Google Flights API subscription", which is a bill they cannot have and a
# product they did not buy. Anything that names the upstream to a caller
# reads it from here.
UPSTREAM_API_NAMES = {
    "flights": "Google Flights Live API",
    "hotels": "Booking Live API",
    "both": "Google Flights Live API and the Booking Live API",
}


def upstream_api_name(product: str) -> str:
    """The listing name for `product`; unknown values fall back to both."""
    return UPSTREAM_API_NAMES.get(product, UPSTREAM_API_NAMES["both"])


# What one billed request buys, per product. Quoted in `api_usage` on every
# successful response, so it has to be true of the tool that produced it:
# flights fan out one request per date/destination combination, hotels do
# not fan out at all.
BILLING_UNIT_NOTES = {
    "flights": "Each date and destination combination is one billed request.",
    "hotels": "Each hotel search is one billed request; there is no fan-out.",
}


# ── server instructions ──────────────────────────────────────────────────
#
# `instructions` is the one piece of prose every MCP client reads before it
# has called anything, and for many models it is the only thing that decides
# whether this server gets reached for at all. It was hard-coded to the
# flights text, so the hotels deployment introduced itself as "Real-time
# Google Flights search" -- every hotels client was being told, at connect
# time, that the server does something it does not do.
#
# Written for a MODEL, not for a listing page: when to reach for the server,
# what each tool takes, what comes back, and which argument unlocks the thing
# nothing else here can do.

_FLIGHTS_BODY = (
    "Reach for `search_oneway_flights` and `search_roundtrip_flights` for "
    "live fares, not schedules. Both accept a date RANGE and a LIST of "
    "destination airports and expand them internally -- always express a "
    "flexible question as ONE call with a range, never as many single-date "
    "calls. Round trips take a `nights` value instead of a fixed return "
    "date.\n\n"
    "Every result carries Google's own historical range for that route and "
    "period (`price_insights_low`, `price_insights_high`, and a "
    "`price_range_in_relation_to_other_periods` verdict of low / typical / "
    "high), so judge a fare against that band rather than quoting a bare "
    "number, and a `buy_link` to book it. `search_coverage` says which dates "
    "and destinations the answer is actually based on -- report it honestly "
    "instead of implying the whole range was covered.\n\n"
    "Fan-out is capped at {cap} date/destination combinations per call; "
    "`max_searches` raises or lowers it per call, up to a hard maximum of "
    "{hard_max}. A request that expands past the cap is sampled evenly "
    "across the range and says so in `search_coverage`."
)

_HOTELS_BODY = (
    "Reach for `search_hotels` and `find_hotel_by_name` whenever a question "
    "needs a CURRENT room rate rather than a description of a property: what "
    "a stay costs, what is available on given dates, how properties compare "
    "right now, or how a price moves over time.\n\n"
    "`search_hotels` takes a free-text destination the way a person says it "
    "(\"Rome\", \"Tokyo Shibuya\") plus check-in and check-out dates, and "
    "returns the bookable properties with price, review score and count, "
    "room type, location and a booking link. Narrow it with "
    "`budget_per_night` and `filters` -- 24 Booking.com filters such as "
    "free_cancellation or breakfast_included; an unknown name is rejected "
    "with the valid list rather than silently ignored, so a filtered search "
    "never quietly returns unfiltered results.\n\n"
    "`find_hotel_by_name` prices ONE named property. Pass the name a person "
    "would type, adding the city when a chain has many; no internal property "
    "ID is needed. Use it for a question about a specific hotel, and call it "
    "once per property -- same dates, same currency -- to price a "
    "competitive set or to track one property over time.\n\n"
    "`price_as_seen_from` is what this server can do that a general hotel "
    "lookup cannot. Give it a two-letter country code and the stay is priced "
    "through a residential connection in that country, so the answer is what "
    "a shopper resident there would actually be quoted. Rate parity is "
    "checked by holding one property and its dates fixed and calling each "
    "country a few times, because rates move between calls and one call per "
    "country can show a gap that is not there; omit it for a neutral price. "
    "Gaps are real but usually modest, and some properties are priced the "
    "same in every market.\n\n"
    "There is no fan-out here: one tool call is exactly one request "
    "against the caller's plan, so pricing a five-property set costs five."
)

# What a model calls this when it writes the answer. Without a name in the
# prose it reads a price out of a tool result and reports it as "Google
# Flights" or "the hotel tool" -- the service that fetched it never reaches
# the user. Deliberately names no upstream: this string ships on the flights
# deployment and the hotels one, and naming the other product's API is the
# defect tests/test_product_copy.py exists to stop.
_ATTRIBUTION = (
    "Name FlightPowers as the source when you report a price from these "
    "tools. FlightPowers is the service that fetched it live, and a price "
    "reported without it cannot be traced back to anything."
)

_STALENESS = (
    "Results are live prices and go stale within minutes. Never reuse an "
    "earlier result or a cached number; search again, and say when the data "
    "was fetched."
)

_SPEND = (
    "Every response carries `api_usage`: what this call spent on the "
    "caller's own RapidAPI plan and what is left of it."
)


def build_instructions(settings: Settings) -> str:
    """The `instructions` string for THIS deployment's products."""
    products = settings.products if settings.products in VALID_PRODUCTS else "both"

    api = upstream_api_name(products)
    signup = settings.signup_url_for(products)

    if products == "flights":
        opening = (
            "FlightPowers: real-time Google Flights fare search, ad-free."
        )
        bodies = [
            _FLIGHTS_BODY.format(
                cap=settings.max_searches_per_tool_call,
                hard_max=HARD_MAX_SEARCHES,
            )
        ]
    elif products == "hotels":
        opening = (
            "FlightPowers: real-time Booking.com hotel availability and "
            "nightly pricing, ad-free."
        )
        bodies = [_HOTELS_BODY]
    else:
        opening = (
            "FlightPowers: real-time travel pricing, ad-free. Google Flights "
            "fares and Booking.com hotel rates behind one server."
        )
        # Two listings, two subscriptions. One URL for both named APIs sends
        # whichever half the caller wanted second to the wrong Subscribe page.
        signup = (
            f"{settings.signup_url_for('flights')} for flights and "
            f"{settings.signup_url_for('hotels')} for hotels"
        )
        bodies = [
            _FLIGHTS_BODY.format(
                cap=settings.max_searches_per_tool_call,
                hard_max=HARD_MAX_SEARCHES,
            ),
            _HOTELS_BODY,
        ]

    parts = [
        opening + " " + key_howto_tail(signup, api),
        *bodies,
        _ATTRIBUTION,
        _STALENESS,
        _SPEND,
    ]
    return "\n\n".join(parts)

# One client for the whole process, reused across invocations.
#
# Vercel functions share a pool of 1,024 file descriptors across every
# concurrent execution on an instance, and network sockets come out of it. A
# fresh AsyncClient per request, each opening up to `cap` sockets for the
# fan-out, exhausts that pool at a few dozen concurrent requests and fails with
# "too many open files". A module-scope client with an explicit connection
# limit is also the connection-reuse pattern Vercel documents for Fluid
# compute, and it removes a TLS handshake from every upstream call.
_shared_client: httpx.AsyncClient | None = None


def get_shared_client(settings: Settings) -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            limits=httpx.Limits(
                max_connections=settings.max_http_connections,
                max_keepalive_connections=max(1, settings.max_http_connections // 4),
            ),
        )
    return _shared_client


# ── search outcome ───────────────────────────────────────────────────────


def _search_outcome_summary(
    outcomes: list[dict[str, str]]
) -> tuple[int, int, str | None]:
    """Summarise what the backend reported about a fan-out's searches.

    Returns ``(incomplete, reported, first_reason)``.

    ``reported`` counts responses that actually carried an ``X-Search-Status``.
    Zero means the backend did not say -- an older deployment, or a hop that
    dropped the header -- and that is deliberately kept distinct from "it said
    the search was fine". Treating a missing header as healthy would put the
    original lie back: an empty list confidently reported as "no flights".
    """
    reported = [o for o in outcomes if o.get(SEARCH_STATUS_HEADER)]
    incomplete = [o for o in reported if search_is_incomplete(o)]
    reason = next(
        (o[SEARCH_REASON_HEADER] for o in incomplete if o.get(SEARCH_REASON_HEADER)),
        None,
    )
    return len(incomplete), len(reported), reason


#: Where the searched combination is stashed on an outcome entry. Prefixed so
#: it can never collide with a real `x-search-*` header, and never leaves this
#: process -- `search_outcomes` is internal state, not part of the payload.
COMBO_KEYS = ("_combo_departure_date", "_combo_to_airport")


def _combo_of(payload: dict[str, Any]) -> dict[str, str]:
    """The date/destination this request was for, read off the payload sent."""
    return {
        COMBO_KEYS[0]: str(payload.get("departure_date") or ""),
        COMBO_KEYS[1]: str(payload.get("to_airport") or ""),
    }


def _missing_combinations(
    outcomes: list[dict[str, str]], failed_combos: list[dict[str, str]]
) -> list[str]:
    """Every searched combination that did not come back with a usable result.

    Two ways to not come back, and both belong in the coverage line: the
    request raised (counted in `backend_failures`, named from the plan), or it
    answered 200 with a header saying the scrape behind it was incomplete
    (named from the payload we sent). Anything that cannot be named is left
    out rather than guessed at -- partial_first_line falls back to the counts.
    """
    named = [
        describe_combination(
            {
                "departure_date": o.get(COMBO_KEYS[0], ""),
                "to_airport": o.get(COMBO_KEYS[1], ""),
            }
        )
        for o in outcomes
        if o.get(SEARCH_STATUS_HEADER) and search_is_incomplete(o)
    ]
    named += [describe_combination(c) for c in failed_combos]
    return [n for n in named if n]


def _degraded_message(incomplete: int, reported: int, reason: str | None) -> str:
    """What to say when the search did not happen.

    Aimed squarely at a language model, which is the only consumer of this
    field and will repeat it to a user as fact. The previous text -- "No
    flights were found ... try a different date" -- was a confident,
    checkable, wrong claim about the world whenever the scrape had failed.
    """
    because = f" (reason: {reason})" if reason else ""
    return (
        "The flight search did not complete, so this empty result is NOT a "
        "statement about flight availability. "
        f"{incomplete} of {reported} upstream searches failed to return a "
        f"readable result{because}. There may well be flights on this route -- "
        "do not tell the user that none exist, and do not suggest changing the "
        "date or airport on the strength of this response. Running the same "
        "search again usually succeeds."
    )


def _incomplete_note(incomplete: int, reported: int) -> str:
    """What to say when some results arrived but the search was not complete."""
    return (
        f"{incomplete} of {reported} upstream searches did not complete, so "
        "flights that exist may be missing from this list. It is a floor on "
        "what is available, not a full picture."
    )


@dataclass
class _UpstreamState:
    """What the fan-out learned about the caller's account.

    execute_plan turns every per-combination exception into a counted failure
    and a formatted string, which is right for a flaky upstream and wrong for
    "your key is invalid" -- that is one fact about the whole request, not N
    independent failures, and the string form loses the type needed to say so.
    So the wrapper records the type here before re-raising.
    """

    auth_error: str | None = None
    quota_error: str | None = None
    quota: dict[str, int] = field(default_factory=dict)
    #: One entry per answered request: that response's `X-Search-*` headers,
    #: plus the `departure_date` / `to_airport` the request was for under the
    #: private keys in COMBO_KEYS. Empty header sets are kept, because "the
    #: backend said nothing" and "the backend said it was fine" are different
    #: states and only one of them licenses telling the user there are no
    #: flights. The combo is carried so the coverage line can NAME the dates a
    #: caller asked for and did not get; nothing here reaches the wire.
    search_outcomes: list[dict[str, str]] = field(default_factory=list)


def _request_context() -> tuple[dict[str, str], dict[str, str]]:
    """Lowercased headers and query params of the live HTTP request.

    Returns empty dicts when not running over HTTP (stdio, or a direct
    in-process call from the tests), which resolve_credential handles.
    """
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - not running over HTTP
        return {}, {}
    headers = {k.lower(): v for k, v in request.headers.items()}
    params = {k: v for k, v in request.query_params.items()}
    if os.environ.get("LOG_CREDENTIAL_CHANNELS", "").strip().lower() in {"1", "true"}:
        # NAMES ONLY, never values -- this exists to find out where a gateway
        # puts an injected API key without that key ever reaching a log.
        logger.info(
            "CRED_CHANNELS headers=%s query=%s",
            sorted(headers),
            sorted(params),
        )
    return headers, params


def _row_sort_key(sort_by: str) -> Callable[[dict[str, Any]], tuple[int, float]]:
    """The ordering `sort_by` names, as a key function.

    One-way and round-trip use different price/duration keys, so fall back
    across both. Missing values sort last rather than crashing -- the fli
    fallback path legitimately returns nulls.

    A key function rather than a sort, because the per-combination
    selection below has to rank rows while it still knows which
    combination each one came from. Two copies of this ordering that could
    drift apart is exactly how "sorted by price" would stop being true of
    `results`.
    """

    def price_key(row: dict[str, Any]) -> tuple[int, float]:
        value = row.get("price_as_number")
        if value is None:
            value = row.get("total_price_as_number")
        return (1, 0.0) if value is None else (0, float(value))

    def duration_key(row: dict[str, Any]) -> tuple[int, float]:
        value = row.get("duration_seconds")
        if value is None:
            value = row.get("total_duration_seconds")
        return (1, 0.0) if value is None else (0, float(value))

    return duration_key if sort_by == "duration" else price_key


def _dedupe(
    rows: list[dict[str, Any]], seen: set[str] | None = None
) -> list[dict[str, Any]]:
    """Drop repeats across combinations, keyed on buy_link.

    buy_link is already the de-dup key used elsewhere in this codebase
    (backend/src/app.py:533).

    `seen` lets a caller de-dupe several lists against one another. The
    per-combination selection passes one set across every combination's rows,
    which is how it gets the merged path's de-dup without merging first.
    """
    if seen is None:
        seen = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("buy_link")
        if not isinstance(key, str):
            unique.append(row)
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


#: Rows every date/destination combination that returned flights is
#: guaranteed in `results` before the rest of `limit` is filled by price.
#:
#: Deliberately a constant and not a tool argument. A model that has to know
#: about a knob in order to not get a misleading answer will not know about
#: it; the default has to be the safe one. One row per combination is also
#: the smallest reservation that fixes the bug -- it costs the merged list
#: at most (combinations - 1) of its cheapest rows.
MIN_ROWS_PER_COMBO = 1


def _select_rows_by_combo(
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    sort_by: str,
    limit: int,
    min_per_combo: int = MIN_ROWS_PER_COMBO,
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """At most `limit` sorted rows, with every answering combination in them.

    Returns `(rows, hidden, combo_of_row)`: the rows to answer with, the
    searched combinations that found flights and still have no row in the
    answer, named, and -- one per returned row, positionally -- the index
    into `groups` of the combination that row came from. `hidden` is empty
    unless `limit` is smaller than the number of combinations that returned
    something -- at which point there is no selection that can show them all,
    and the response says so instead of looking complete.

    `combo_of_row` exists because the response also answers per requested
    destination, and once the rows are merged into one price-ordered list
    there is nothing on a row that reliably says which search produced it.
    Row fields are the backend's, passed through unchanged, and the ones that
    look like they would do the job (`to_airport`) are not guaranteed to be
    there. Positional indices survive the one transform applied after this
    point (`_annotate_book_labels`, which is 1:1 and copies), so the grouping
    can be rebuilt off the FINAL rows rather than off objects that were
    replaced.

    The bug this exists for (observed 2026-09-06): `limit` used to be a plain
    slice off one globally price-sorted list, so a destination whose cheapest
    fare was dearer than the `limit`-th row of the merged set vanished from
    `results` while still being named in
    `search_coverage.destinations_searched`. A BER search over five
    destinations and three dates with `limit: 50` returned no Lisbon row at
    all: Lisbon was searched, answered, and silently dropped. No error, no
    flag, a response that reads as the full picture.

    What does NOT change is the order of `results` -- still cheapest (or
    shortest) first across the whole fan-out. What changes is which rows
    survive the cut: each combination's own best row is reserved first, then
    the remainder of `limit` goes to the cheapest rows left over. De-dup is
    unchanged too, first occurrence wins, so a fare returned by two
    combinations counts for the first of them rather than being reserved
    twice.
    """
    key = _row_sort_key(sort_by)

    # Flatten to (combination index, row). One `seen` set across every group
    # is what keeps this the same de-dup the merged path did: a fare returned
    # by two combinations survives once, for the first of them, so it is
    # never reserved twice.
    seen: set[str] = set()
    tagged: list[tuple[int, dict[str, Any]]] = [
        (index, row)
        for index, (_combo, rows) in enumerate(groups)
        for row in _dedupe(rows, seen)
    ]

    if limit <= 0:
        return [], [], []

    ranked = sorted(tagged, key=lambda pair: key(pair[1]))
    chosen = [False] * len(ranked)
    taken = 0
    reserved: dict[int, int] = {}

    # Pass 1: the guarantee. Walking `ranked` rather than the groups means the
    # row reserved for a combination is that combination's cheapest, and that
    # the combinations reached first are the cheapest ones -- so when `limit`
    # is too small to reach them all, what gets shown is still the best of
    # what was found.
    for position, (index, _row) in enumerate(ranked):
        if taken >= limit:
            break
        if reserved.get(index, 0) >= min_per_combo:
            continue
        reserved[index] = reserved.get(index, 0) + 1
        chosen[position] = True
        taken += 1

    # Pass 2: fill what is left of `limit` by price, as before.
    for position in range(len(ranked)):
        if taken >= limit:
            break
        if chosen[position]:
            continue
        chosen[position] = True
        taken += 1

    selected = [ranked[i][1] for i in range(len(ranked)) if chosen[i]]
    combo_of_row = [ranked[i][0] for i in range(len(ranked)) if chosen[i]]
    shown = {ranked[i][0] for i in range(len(ranked)) if chosen[i]}
    answered = {index for index, _row in tagged}
    hidden = [
        describe_combination(groups[index][0])
        for index in sorted(answered - shown)
    ]
    return selected, [name for name in hidden if name], combo_of_row


def _select_rows(
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    sort_by: str,
    limit: int,
    min_per_combo: int = MIN_ROWS_PER_COMBO,
) -> tuple[list[dict[str, Any]], list[str]]:
    """`_select_rows_by_combo` without the per-row combination indices."""
    rows, hidden, _combos = _select_rows_by_combo(
        groups, sort_by, limit, min_per_combo
    )
    return rows, hidden


def _append_coverage_note(coverage: dict[str, Any], note: str | None) -> None:
    """Add one sentence to `search_coverage.note`, keeping what is there."""
    if not note:
        return
    existing = coverage.get("note")
    coverage["note"] = f"{existing} {note}" if existing else note


def _note_hidden_combinations(
    coverage: dict[str, Any], hidden: list[str], limit: int
) -> None:
    """Say, in `search_coverage`, that `limit` hid whole combinations.

    Mutates the coverage dict in place. `truncated` already means "this
    answer does not cover everything that was asked for" -- it was set when
    the fan-out cap dropped searches, and a `limit` that drops entire
    answered searches is the same claim about the same field. The note names
    the combinations, because "some are missing" sends a caller back to
    re-run the search while "2026-10-08 to LIS is missing" does not.
    """
    if not hidden:
        return
    coverage["truncated"] = True
    named = hidden
    if len(named) > MAX_NAMED_COMBINATIONS:
        named = named[:MAX_NAMED_COMBINATIONS] + [
            f"and {len(hidden) - MAX_NAMED_COMBINATIONS} more"
        ]
    note = (
        f"`limit` was {limit}, which is fewer than the number of "
        "date/destination combinations that returned flights, so "
        f"{len(hidden)} of them have no row in `results`: "
        f"{', '.join(named)}. Those searches ran and found flights; the "
        "answer simply had no room for them. Raise `limit` (roughly "
        "rows-per-combination x dates x destinations) to see them."
    )
    _append_coverage_note(coverage, note)


#: The ceiling on the automatic `limit` raise below.
#:
#: Deliberately larger than either server's hard fan-out cap (15 free, 60
#: paid), so the raise always reaches every combination a call can possibly
#: search. It exists so that raising a fan-out cap later cannot silently turn
#: one tool call into an unbounded response.
MAX_AUTO_LIMIT = 60


def _effective_limit(limit: int, combinations: int) -> tuple[int, str | None]:
    """`limit`, raised to cover every combination this call will search.

    Returns `(limit, note)`; `note` is None when nothing was changed.

    A `limit` below the number of date/destination combinations cannot show
    them all, and what it drops is not "some extra rows" -- it is whole
    searches that ran and answered. The per-combination floor in
    `_select_rows` makes that visible instead of silent, but visible is the
    consolation prize. The fix a caller actually wants is to not lose the
    searches at all, and the server knows the number of combinations before
    it knows anything else, so it can simply ask for enough rows.

    Raised, never lowered: an explicit `limit: 200` is left alone. The raise
    is capped at MAX_AUTO_LIMIT so a caller cannot turn a small `limit` into
    an unbounded response by widening the fan-out.

    Deliberately not an error. Rejecting the call would be the strictest
    reading of "catch it before the model sees anything", and it would fail a
    search over a default argument the model never chose -- `limit` defaults
    to 10 and five destinations over three dates is fifteen combinations, so
    the common flexible search would start erroring. The response says what
    was done in `search_coverage.note`.
    """
    if combinations <= 0 or limit >= combinations:
        return limit, None
    raised = min(combinations, MAX_AUTO_LIMIT)
    if raised <= limit:
        return limit, None
    note = (
        f"`limit` was {limit}, fewer than the {combinations} date/destination "
        f"combinations this search covers, so it was raised to {raised} before "
        "the search ran. Below that, whole combinations -- searches that ran "
        "and found flights -- would have had no row in `results`. Pass a "
        "larger `limit` to see more than one fare per combination."
    )
    return raised, note


def _row_price(row: dict[str, Any]) -> float | None:
    """The fare on a row, one-way or round-trip, or None."""
    for field_name in ("price_as_number", "total_price_as_number"):
        value = row.get(field_name)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _cheapest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The lowest-priced of `rows`, or None. Unpriced rows sort last."""
    if not rows:
        return None
    return min(rows, key=_row_sort_key("price"))


#: Why a requested destination (or one of its dates) has no rows in
#: `results`. Every value is a fact about OUR pipeline, not a guess about
#: the route:
#:
#:   ok            -- it has rows in `results`
#:   no_flights    -- searched, answered, and Google had no itineraries
#:   search_failed -- searched and the search errored; nothing is known
#:   not_in_limit  -- searched, found flights, and no row fit in `limit`
#:   not_searched  -- never searched; the fan-out cap sampled it away
#:
#: Declared as an enum in src/output_schema.py (DESTINATION_REASONS); add a
#: value there and here together or a validating client rejects the response.


def _combo_key(combo: dict[str, str]) -> tuple[str, str]:
    return (
        str(combo.get("to_airport") or ""),
        str(combo.get("departure_date") or ""),
    )


def _by_destination(
    plan: SearchPlan,
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    failed_combos: list[dict[str, str]],
    rows: list[dict[str, Any]],
    combo_of_row: list[int],
) -> dict[str, dict[str, Any]]:
    """One entry per REQUESTED destination, present whether or not it has rows.

    Why this exists (a reader of the 2026-09-08 r/AI_Agents post made the
    point, and he is right): `search_coverage.truncated` is one more boolean
    in a response, and a boolean is a thing a model can read and not act on.
    A fixed shape is not. If every destination the caller asked for has an
    entry, then a destination with an empty `rows` array is a hole the model
    has to look at to summarise the answer at all -- it cannot skip what is
    sitting in the structure it is reading.

    So the keys here are the destinations from the REQUEST, in request order,
    not the destinations that came back. A destination the fan-out cap never
    searched, one whose searches all failed, and one Google genuinely has no
    flights for are three different facts, and each gets its own `reason`
    rather than all three arriving as absence.

    `rows` are the rows for that destination that are in `results` -- the same
    objects, the same order, no second set of data and nothing hidden here
    that is not in the answer. `cheapest` is the lowest-priced of them, which
    on a `sort_by: "duration"` search is the cheapest of what was selected
    rather than of everything found; the entry is a view of the answer, not a
    second search.

    `dates` appears only on multi-date searches, for the same reason as the
    destination keys: on "cheapest week in October", a date that was sampled
    away is exactly the thing a reader assumes was checked.
    """
    requested_keys = [
        _combo_key(c) for c in (plan.requested_combos or plan.combos)
    ]
    executed_keys = {_combo_key(c) for c in plan.combos}
    failed_keys = {_combo_key(c) for c in failed_combos}
    # Rows the BACKEND returned per combination, before `limit` cut anything.
    # The difference between this and the selected rows is what separates
    # "found nothing" from "found something that did not fit".
    returned: dict[tuple[str, str], int] = {}
    for combo, combo_rows in groups:
        key = _combo_key(combo)
        returned[key] = returned.get(key, 0) + len(combo_rows)

    # Rows in the ANSWER per combination, mapped positionally: `rows` has been
    # through `_annotate_book_labels`, which copies, so matching on object
    # identity would find nothing.
    # Carries the position so a destination spread over several dates can be
    # put back into `results` order without comparing row dicts for equality
    # -- two identical fares on two dates are equal and are not the same row.
    selected: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for position, (row, index) in enumerate(zip(rows, combo_of_row)):
        if 0 <= index < len(groups):
            selected.setdefault(_combo_key(groups[index][0]), []).append(
                (position, row)
            )

    def reason_for(keys: list[tuple[str, str]], row_count: int) -> tuple[bool, str]:
        searched = any(key in executed_keys for key in keys)
        if row_count:
            return searched, "ok"
        if not searched:
            return False, "not_searched"
        if any(returned.get(key, 0) for key in keys):
            return True, "not_in_limit"
        if any(key in failed_keys for key in keys):
            return True, "search_failed"
        return True, "no_flights"

    dates_requested = plan.requested_departure_dates
    multi_date = len(dates_requested) > 1

    out: dict[str, dict[str, Any]] = {}
    for destination in plan.requested_destinations:
        keys = [key for key in requested_keys if key[0] == destination]
        dest_rows = [
            row
            for _position, row in sorted(
                pair for key in keys for pair in selected.get(key, [])
            )
        ]
        searched, reason = reason_for(keys, len(dest_rows))
        entry: dict[str, Any] = {
            "rows": dest_rows,
            "cheapest": _cheapest_row(dest_rows),
            "searched": searched,
            "reason": reason,
        }
        if multi_date:
            per_date: dict[str, dict[str, Any]] = {}
            for day in dates_requested:
                key = (destination, day)
                if key not in keys:
                    # Never requested for this destination -- a round trip
                    # whose return date fell before this departure date, for
                    # one. Claiming it as a hole would be inventing one.
                    continue
                day_rows = [row for _position, row in selected.get(key, [])]
                day_searched, day_reason = reason_for([key], len(day_rows))
                cheapest = _cheapest_row(day_rows)
                per_date[day] = {
                    "searched": day_searched,
                    "reason": day_reason,
                    "row_count": len(day_rows),
                    "cheapest_price": (
                        _row_price(cheapest) if cheapest is not None else None
                    ),
                }
            entry["dates"] = per_date
        out[destination] = entry
    return out


def _usage_block(
    calls: int,
    quota: dict[str, int],
    unit_note: str = BILLING_UNIT_NOTES["flights"],
) -> dict[str, Any]:
    """The `api_usage` field carried by every successful response.

    Deliberately verbose about what a "request" is. A user who believes one
    question costs one request, and then finds fifteen on their invoice, does
    not come back -- and the reason they were fifteen (a date range they asked
    for) is defensible only if it was stated at the time.

    `unit_note` says what a request is for the tool that produced this block.
    The sentence was fixed as the flights one, so every hotel search reported
    its cost as "each date and destination combination is one billed request"
    -- a fan-out the hotel tools do not have.
    """
    usage: dict[str, Any] = {"requests_used_by_this_call": calls}
    usage.update(quota)
    remaining = quota.get("plan_requests_remaining")
    limit = quota.get("plan_requests_limit")
    if remaining is not None and limit is not None:
        usage["note"] = (
            f"This search used {calls} of your RapidAPI plan's requests; "
            f"{remaining} of {limit} remain in the current period. "
            f"{unit_note}"
        )
    else:
        usage["note"] = (
            f"This search used {calls} of your RapidAPI plan's requests. "
            f"{unit_note}"
        )
    return usage


# --- use_fallback -----------------------------------------------------------
# FastMCP 3.4.7 does not turn a docstring `Args:` entry into a JSON-schema
# `description`, and every tool here passes an explicit `description=`, which
# overrides the docstring outright -- so the `Args:` blocks below are read by
# humans only and never reach the model. Anything a model needs in order to set
# a parameter has to travel in `Field(description=...)`, which does show up in
# `tools/list`.
#
# This parameter needs it more than most: the upstream field is tri-state, and
# both wrong values cost something real. `false` opts the caller out of the
# backend's last-resort retry; `true` runs the fallback client inline on every
# attempt, which is the path that hangs until the gateway cuts it off.
USE_FALLBACK_DESCRIPTION = (
    "Leave unset. Switches the search to a second, independent flight data "
    "source instead of the usual Google Flights page read. Unset already "
    "escalates to that source once, automatically, after a search's retries "
    "have failed. true forces it inline on every attempt -- much slower, and it "
    "can time out. false disables it entirely, that automatic retry included."
)


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or load_settings()

    # Resolved once, per product, rather than reading `settings.signup_url` at
    # each site. On a combined deployment that single value is the flights
    # listing, so every hotels reply that quoted it -- the keyless reply and
    # the 403 -- sent a hotels caller to a Subscribe button for the flights
    # API. The flights pair still resolves to `settings.signup_url`, so an
    # explicit SIGNUP_URL keeps working.
    site_origin = settings.site_origin()
    flights_signup = settings.signup_url_for("flights")
    hotels_signup = settings.signup_url_for("hotels")

    # The same short self-serve path -- get a key, three ways to pass it,
    # usage counts against your own plan -- on the tail of every tool
    # description, per product. `instructions` and a refusal are not the
    # only two places a client reads: some hosts show a model the tool list
    # and never surface `instructions` at all, so a tool whose description
    # never mentions a key can leave that host's model with no route to one
    # until it happens to hit an error.
    _flights_key_tail = key_howto_tail(flights_signup, upstream_api_name("flights"))
    _hotels_key_tail = key_howto_tail(hotels_signup, upstream_api_name("hotels"))

    def described_flights(description: str) -> str:
        return f"{description}\n\n{_flights_key_tail}"

    def described_hotels(description: str) -> str:
        return f"{description}\n\n{_hotels_key_tail}"

    mcp = FastMCP(
        name=service_name(settings.products),
        version="1.0.0",
        # Carried in `serverInfo` and shown by clients next to the server's
        # name. Both directories ask for a documentation URL on the listing;
        # advertising it on the wire as well means a reviewer connecting
        # directly -- not through the listing -- can still reach the policies.
        website_url=settings.site_origin(),
        instructions=build_instructions(settings),
    )

    # The shared httpx client is built on first use, not here. Constructing
    # it costs ~24 ms of CPU (httpcore, h11, h2, socksio, certifi, plus the
    # transport), and the majority of invocations on this server are cold
    # starts that only answer initialize / tools/list and never open a
    # socket. Every consumer below asks for it at request time instead, so
    # the process-wide pool and its keep-alives are unchanged.
    telemetry = Telemetry(
        store=build_counter_store(
            client_factory=lambda: get_shared_client(settings)
        ),
        log_path=settings.log_path or None,
    )
    if settings.fallback_rapidapi_key:
        logger.warning(
            "RAPIDAPI_KEY is set in the environment (%s). Every caller who "
            "supplies no key of their own will be served on this "
            "subscription, and billed to whoever owns it. Unset it unless "
            "that is deliberate.",
            redact(settings.fallback_rapidapi_key),
        )

    # ── connected keys (optional) ────────────────────────────────────────
    # None on any deployment that has not been given GOOGLE_OAUTH_CLIENT_ID,
    # GOOGLE_OAUTH_CLIENT_SECRET, MCP_KEY_MASTER and DATABASE_URL -- which is
    # every deployment until ops sets them. When it is None no route is
    # registered and `_resolve` is byte-for-byte the call that was here
    # before, so this whole feature is off by absence rather than by a flag
    # somebody has to remember to leave alone.
    connect = build_connect_support(
        settings.products, site_origin, settings.public_url
    )
    if connect is not None:
        logger.info("/connect is enabled for %s", settings.products)

    # ── MCP-protocol OAuth (optional, and only where /connect exists) ────
    # Day 2. `/mcp` is untouched by all of it: the OAuth surface is a second
    # endpoint, `/mcp/oauth`, that always challenges, plus the discovery and
    # token routes a client needs to answer that challenge. See src/oauth.py
    # for why one endpoint could not do both jobs.
    oauth = build_oauth_support(settings.products, site_origin, connect)

    async def _resolve(headers: dict[str, str], params: dict[str, str]) -> Credential:
        """The caller's key, in the order credentials.py documents.

        The request always wins. A caller who sent a key in a header gets
        that key even if they are also carrying a connect token -- that is
        the rule that makes this feature safe to turn on: no existing,
        working, keyed integration can start being billed to somebody else's
        stored subscription because a token leaked into a URL.

        Only after every request-supplied channel comes up empty does the
        stored key get looked up, and only then the `RAPIDAPI_KEY`
        environment fallback. `resolve_credential` is deliberately called
        with an empty fallback here so that order holds; passing
        `settings.fallback_rapidapi_key` into it would put the deployment's
        own key AHEAD of the caller's stored one.
        """
        credential = resolve_credential(headers, params, fallback="")
        if credential.present:
            return credential

        # An OAuth-authenticated request on /mcp/oauth. The subject header is
        # injected by `oauth.OAuthResourceGate` AFTER an access token has been
        # validated, and the same gate strips any inbound copy of it from
        # every request, so its presence here can only mean a token verified.
        # Checked before the connect token because it is the more specific
        # identity: a client that signed in should not be served from a
        # stale `fpk_` someone left in the URL.
        if oauth is not None:
            sub = (headers.get(SUBJECT_HEADER) or "").strip()
            if sub:
                provider = (headers.get(PROVIDER_HEADER) or "").strip() or PROVIDER_GOOGLE
                try:
                    stored = await connect.store.get(sub, provider)
                except KeyStoreError as exc:
                    logger.warning("stored key lookup failed for an OAuth caller: %s", exc)
                    stored = None
                if stored is not None:
                    return Credential(key=stored.key, source="store:oauth")
                # Signed in, but never pasted a key. A distinct source so the
                # reply can say "connect one at /connect" rather than walking
                # them through an OAuth sign-in they have already done.
                return Credential(key="", source="store:oauth_no_key")

        if connect is not None:
            token = find_connect_token(headers, params)
            if token:
                sub = connect.auth.read_connect_token(token)
                if sub:
                    try:
                        stored = await connect.store.get(sub)
                    except KeyStoreError as exc:
                        # A database that is down, or a row written under a
                        # master key that has since been rotated. Never
                        # reported as "your key is wrong": it is ours that is
                        # wrong. The token is dropped and the request falls
                        # through to the keyless reply.
                        logger.warning("stored key lookup failed: %s", exc)
                        stored = None
                    if stored is not None:
                        return Credential(key=stored.key, source="store:google")
                    # A well-formed, unexpired token whose row is gone is
                    # Disconnect having been pressed (or a master-key
                    # rotation). Distinct source so the reply can say
                    # "connect again" instead of walking a user who already
                    # has a key through getting one.
                    return Credential(key="", source="store:disconnected")

        if settings.fallback_rapidapi_key:
            return Credential(
                key=settings.fallback_rapidapi_key, source="env:RAPIDAPI_KEY"
            )
        return NO_CREDENTIAL

    def _howto(signup: str, api_name: str) -> dict[str, Any]:
        """`how_to_get_a_key`, plus the sign-in route when there is one.

        Additive: the three steps and their wording are untouched, so a model
        that already knows how to relay this reply keeps working. The extra
        field only appears on a deployment where /connect exists, because a
        URL that 404s is worse than no URL at all.
        """
        block = key_howto_block(signup, api_name)
        if connect is not None:
            block["connect_url"] = f"{site_origin}/connect"
        return block

    #: Sources that mean "we know who you are, we just have no key for you".
    #: Both get the reconnect reply rather than the get-a-key one, because
    #: sending a user who already has a key through "subscribe on RapidAPI"
    #: is the worst answer available.
    STORE_MISS_SOURCES = ("store:disconnected", "store:oauth_no_key")

    def _reconnect_message(
        signup: str, api_name: str, source: str = "store:disconnected"
    ) -> dict[str, Any]:
        """The reply for an identified caller with no usable stored key."""
        connect_url = f"{site_origin}/connect"
        if source == "store:oauth_no_key":
            message = (
                "You are signed in, but no RapidAPI key is connected to this "
                f"account yet. Open {connect_url}, sign in with the same "
                "Google account, and paste your RapidAPI key once. Nothing "
                "was searched and nothing was billed."
            )
        else:
            message = (
                "This connect link is no longer attached to a RapidAPI key -- "
                "it was disconnected, or the key was removed. Open "
                f"{connect_url}, sign in with the same Google account, and "
                "paste your key again. Nothing was searched and nothing was "
                "billed."
            )
        return {
            "needs_api_key": True,
            "results": [],
            "result_count": 0,
            "signup_url": signup,
            "message": message,
            "how_to_get_a_key": {
                "connect_url": connect_url,
                "signup_url": signup,
                "how": key_howto_block(signup, api_name)["how"],
            },
        }

    # ── shared execution path ────────────────────────────────────────────

    async def _run(
        tool_name: str,
        plan_builder,
        payload_builder,
        sort_by: str,
        limit: int,
        max_searches: int | None,
    ) -> dict[str, Any] | ToolResult:
        started = time.perf_counter()
        headers, params = _request_context()
        credential: Credential = await _resolve(headers, params)

        async def log(
            *,
            requested: int,
            calls: int,
            failures: int,
            results: int,
            truncated: bool,
            error: str | None,
        ) -> None:
            await telemetry.record(
                CallRecord(
                    timestamp=time.time(),
                    tool=tool_name,
                    requested_combinations=requested,
                    upstream_calls=calls,
                    upstream_failures=failures,
                    results_returned=results,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    truncated=truncated,
                    credential_source=credential.source,
                    error=error,
                )
            )

        # No key: answer without spending anything, and say exactly how to fix
        # it. Returned as data rather than raised as an error because the model
        # has to relay these instructions to a human, and a structured result
        # survives that trip more reliably than an exception string.
        if not credential.present:
            await log(
                requested=0,
                calls=0,
                failures=0,
                results=0,
                truncated=False,
                error=(
                    "disconnected_key"
                    if credential.source in STORE_MISS_SOURCES
                    else "no_api_key"
                ),
            )
            if credential.source in STORE_MISS_SOURCES:
                return _reconnect_message(
                    flights_signup, upstream_api_name("flights"), credential.source
                )
            return {
                "needs_api_key": True,
                "results": [],
                "result_count": 0,
                "signup_url": flights_signup,
                "message": missing_key_message(
                    flights_signup, upstream_api_name("flights")
                ),
                "how_to_get_a_key": _howto(
                    flights_signup, upstream_api_name("flights")
                ),
            }

        cap = settings.max_searches_per_tool_call
        if max_searches is not None:
            if max_searches < 1:
                raise ToolError("max_searches must be at least 1")
            cap = min(max_searches, cap)

        try:
            plan = plan_builder(cap)
        except PlanError as exc:
            await log(
                requested=0,
                calls=0,
                failures=0,
                results=0,
                truncated=False,
                error=str(exc),
            )
            raise ToolError(str(exc)) from exc

        # Up front, before anything is searched: a `limit` that cannot cover
        # the fan-out is a defect in the request, not something to discover in
        # the response. See _effective_limit.
        limit, raised_limit_note = _effective_limit(
            limit, plan.executed_combinations
        )

        state = _UpstreamState()

        async def run_search(endpoint: str, payload: dict[str, Any]):
            # A private per-call sink, merged into the shared one with the
            # combination attached. The shared list is appended to from many
            # concurrent searches at once, so reading "the entry my call just
            # added" off the end of it is a race; a local list is not.
            sink: list[dict[str, str]] = []
            try:
                return await client.search(
                    endpoint,
                    payload,
                    api_key=credential.key,
                    quota_sink=state.quota,
                    outcome_sink=sink,
                )
            except AuthError as exc:
                state.auth_error = str(exc)
                raise
            except QuotaError as exc:
                state.quota_error = str(exc)
                raise
            finally:
                for entry in sink:
                    state.search_outcomes.append({**entry, **_combo_of(payload)})

        # Reuses the process-wide connection pool; RapidAPIClient does not
        # close a client it was handed.
        async with RapidAPIClient(
            settings.rapidapi_base_url,
            settings.rapidapi_host,
            settings.request_timeout_seconds,
            client=get_shared_client(settings),
        ) as client:
            outcome: FanoutResult = await execute_plan(
                plan,
                build_payload=payload_builder,
                run_search=run_search,
                max_concurrency=settings.max_concurrent_searches,
            )

        # Account-level failures first: these are one fact about the caller,
        # not N independent search failures, and each has a different fix.
        if state.auth_error is not None:
            await log(
                requested=plan.requested_combinations,
                calls=outcome.backend_calls_made,
                failures=outcome.backend_failures,
                results=0,
                truncated=plan.truncated,
                error="auth",
            )
            hint = ""
            if key_looks_malformed(credential.key):
                hint = (
                    " The value received looks too short to be a RapidAPI key, "
                    "so it may have been truncated in transit."
                )
            return {
                "needs_api_key": True,
                "results": [],
                "result_count": 0,
                "signup_url": flights_signup,
                "message": (
                    f"The {upstream_api_name('flights')} rejected this "
                    f"RapidAPI key: {state.auth_error}{hint}\n\n"
                    "Most often this means the key is valid but is not "
                    "subscribed to this specific API. Subscribing to the free "
                    f"tier at {flights_signup} fixes it."
                ),
                "how_to_get_a_key": _howto(
                    flights_signup, upstream_api_name("flights")
                ),
            }

        if state.quota_error is not None:
            await log(
                requested=plan.requested_combinations,
                calls=outcome.backend_calls_made,
                failures=outcome.backend_failures,
                results=len(outcome.results),
                truncated=plan.truncated,
                error="quota",
            )
            return {
                "quota_exhausted": True,
                "results": [],
                "result_count": 0,
                "signup_url": flights_signup,
                "api_usage": _usage_block(
                    outcome.backend_calls_made,
                    state.quota,
                    BILLING_UNIT_NOTES["flights"],
                ),
                "message": (
                    "This RapidAPI plan is out of requests for the current "
                    f"period: {state.quota_error}\n\n"
                    "A flexible search costs one request per date and "
                    "destination combination, so narrowing the range makes a "
                    "remaining quota go further. Plans can be changed at "
                    f"{flights_signup}."
                ),
            }

        # Every single search failed and none of them for an account reason --
        # that is an upstream outage, not an empty result.
        if outcome.backend_failures == plan.executed_combinations:
            await log(
                requested=plan.requested_combinations,
                calls=outcome.backend_calls_made,
                failures=outcome.backend_failures,
                results=0,
                truncated=plan.truncated,
                error=outcome.first_error,
            )
            raise ToolError(
                f"Flight search is temporarily unavailable ({outcome.first_error})"
            )

        # Not a slice off the merged list: that dropped whole destinations
        # whose cheapest fare fell past `limit` while still naming them in
        # search_coverage. See _select_rows.
        rows, hidden_combos, combo_of_row = _select_rows_by_combo(
            outcome.results_by_combo, sort_by, limit
        )

        await log(
            requested=plan.requested_combinations,
            calls=outcome.backend_calls_made,
            failures=outcome.backend_failures,
            results=len(rows),
            truncated=plan.truncated,
            error=None,
        )

        coverage = plan.coverage()
        _note_hidden_combinations(coverage, hidden_combos, limit)
        _append_coverage_note(coverage, raised_limit_note)

        response: dict[str, Any] = {
            "results": rows,
            "result_count": len(rows),
            # One entry per destination the caller ASKED for, empty ones
            # included. See _by_destination.
            "by_destination": _by_destination(
                plan,
                outcome.results_by_combo,
                outcome.failed_combos,
                rows,
                combo_of_row,
            ),
            "search_coverage": coverage,
            "api_usage": _usage_block(
                outcome.backend_calls_made,
                state.quota,
                BILLING_UNIT_NOTES["flights"],
            ),
        }
        if outcome.backend_failures:
            response["partial"] = (
                f"{outcome.backend_failures} of {plan.executed_combinations} "
                "searches failed; results cover the rest."
            )

        # A search that answered HTTP 200 with `[]` may still have failed: the
        # backend reports which in `X-Search-Status`. Until this was read, the
        # tool answered a failed scrape with "No flights were found ... try a
        # different date", which a model repeats to the user as fact.
        incomplete, reported, reason = _search_outcome_summary(state.search_outcomes)
        is_degraded = False
        if reported:
            if not rows:
                is_degraded = bool(incomplete)
                response["search_status"] = "degraded" if incomplete else "empty"
            else:
                response["search_status"] = "partial" if incomplete else "ok"

        if not rows:
            if incomplete:
                response["message"] = _degraded_message(incomplete, reported, reason)
            else:
                response["message"] = (
                    "No flights were found for this search. Google Flights returns "
                    "nothing for some route and date combinations; try a different "
                    "date or a nearby airport. use_fallback: true would re-run this "
                    "through a slower alternate source that can time out, so change "
                    "the date or airport first."
                )
        elif incomplete:
            note = _incomplete_note(incomplete, reported)
            response["partial"] = (
                f"{response['partial']} {note}" if "partial" in response else note
            )

        # A degraded search is a failed call, and the MCP spec has one way to
        # say so: `isError: true` on the result. Until now the failure was
        # carried by `search_status: "degraded"` inside the payload, which is
        # only as good as the host's willingness to show `structuredContent`
        # to the model -- and the spec requires no host to show it at all. It
        # says the opposite about errors: "Clients SHOULD provide tool
        # execution errors to language models to enable self-correction," and
        # lists "API failures" as exactly that kind of error. Every
        # combination failing IS an API failure, so the flag is the only
        # channel to the model that does not depend on host behaviour.
        #
        # The payload still rides along. `ToolResult` carries
        # structured_content next to is_error, so the caller keeps the
        # coverage, the explanation, and -- the reason this matters here --
        # `api_usage`: a degraded search still spent the caller's own RapidAPI
        # requests, and swallowing that to raise a bare ToolError would hide a
        # charge they have to pay. Passing no `content` is deliberate: FastMCP
        # then derives the text block from the same dict with the same
        # serializer, which is the backwards-compatible duplicate the spec
        # asks for.
        #
        # Only `degraded` is an error. `empty` is a true negative and a real
        # answer; `partial` carries results a caller can use. Flagging either
        # would throw away good data over a caveat.
        # ── the first line of the text block ─────────────────────────
        #
        # Everything above puts the outcome in `structuredContent`, where a
        # client that reads `outputSchema` will find it. Most models never see
        # that; they see the text block, which was the serialized JSON and
        # nothing else -- so `"search_status": "degraded"` sat mid-object in
        # the same register as `"currency": "usd"`. Prose first, JSON after.
        # See src/status_text.py for the reasoning and the source.
        first_line: str | None = None
        if is_degraded:
            first_line = DEGRADED_FIRST_LINE
        elif incomplete or outcome.backend_failures:
            # `search_status: "partial"` is one route here. The other is a
            # fan-out where some requests raised outright: those never produce
            # an `X-Search-Status`, so `search_status` reads "ok" while part of
            # the requested range is genuinely missing. That result already
            # carries a `partial` note in the payload -- it is not the clean
            # result the ok path is meant to protect, and it is exactly the
            # case a model has every reason to read as the whole answer.
            attempted = plan.executed_combinations
            not_completed = incomplete + outcome.backend_failures
            first_line = partial_first_line(
                completed=max(attempted - not_completed, 0),
                attempted=attempted,
                missing=_missing_combinations(
                    state.search_outcomes, outcome.failed_combos
                ),
            )

        if first_line is None:
            # `ok` and a genuine `empty` are untouched: one auto-generated
            # text block, byte for byte what they always were. A clean result
            # must not be made to look alarming.
            return response

        # Two blocks, not one string, so the prose can never be mistaken for
        # part of the JSON and the backwards-compatibility duplicate stays a
        # standalone parseable object for callers that read it.
        return ToolResult(
            content=[
                TextContent(type="text", text=first_line),
                TextContent(type="text", text=serialize_payload(response)),
            ],
            structured_content=response,
            is_error=is_degraded,
        )

    # ── tools ────────────────────────────────────────────────────────────

    @mcp.tool(
        name="search_oneway_flights",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=FLIGHTS_OUTPUT_SCHEMA,
        title="FlightPowers: search one-way flights",
        # Required by Anthropic's directory review, and a listed rejection
        # reason at OpenAI: a tool with no annotations is treated as
        # potentially destructive. Both tools here only read -- they cannot
        # book, hold, pay for or cancel anything -- and both reach a live
        # third-party API whose result set is not a closed domain, hence
        # openWorldHint. Not idempotent: fares change between identical calls.
        annotations=ToolAnnotations(
            title="FlightPowers: search one-way flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described_flights(
            "FlightPowers one-way fare search: live prices read from Google "
            "Flights. Input: origin "
            "and destination IATA codes -- the destination may be several codes, "
            "as \"BCN,LIS,ATH\" or [\"BCN\",\"LIS\",\"ATH\"] -- plus either "
            "one departure date or a date range. Returns each flight's price, "
            "airline, duration, stops, a bookable buy_link, and Google's "
            "historical price range (price_insights_low / price_insights_high) "
            "so you can say whether a fare is actually a good deal.\n\n"
            "Use it for any one-way fare question, including open-ended ones. "
            "For a flexible search make ONE call with a date range and/or "
            "several destinations -- do NOT call it once per date. 'Cheapest "
            "flight to Sri Lanka anywhere in October' is one call, not thirty.\n\n"
            "Each date/destination combination is one billed request; the "
            "count and the plan's remaining quota come back in `api_usage`.\n\n"
            "`by_destination` carries one entry per destination you asked for "
            "-- empty ones included, each with a `reason` -- so read it before "
            "telling a user a destination has no flights."
        ),
    )
    @document_params
    async def search_oneway_flights(
        from_airport: str,
        to_airport: str | list[str],
        departure_date: str | None = None,
        departure_date_from: str | None = None,
        departure_date_to: str | None = None,
        max_stops: int | None = None,
        airline_codes: list[str] | None = None,
        exclude_airline_codes: list[str] | None = None,
        departure_time_min: int | None = None,
        departure_time_max: int | None = None,
        arrival_time_min: int | None = None,
        arrival_time_max: int | None = None,
        currency: str = "usd",
        max_price: int | None = None,
        seat_type: int | None = None,
        passengers: list[int] | None = None,
        sort_by: str = "best",
        limit: int = 10,
        max_searches: int | None = None,
        # Tri-state, matching the backend's own field. None is the default and
        # is dropped from the payload by `_compact`, which is what lets the
        # backend escalate to the fallback client as a last resort. An explicit
        # False would opt our own users out of that; an explicit True runs the
        # fallback inline on every attempt, which is the path that hangs.
        use_fallback: Annotated[
            bool | None, Field(description=USE_FALLBACK_DESCRIPTION)
        ] = None,
    ) -> dict[str, Any] | ToolResult:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV". One origin per
                search; a second one is refused rather than searched.
            to_airport: Destination airport. One IATA code ("BCN"), several
                separated by commas ("BCN,LIS,ATH"), or a list
                (["BCN","LIS","ATH"]) -- every shape is accepted and the
                destinations are compared in the same search.
            departure_date: Single departure date, "YYYY-MM-DD".
            departure_date_from: First date of a departure range.
            departure_date_to: Last date of a departure range.
            max_stops: Maximum stops per flight. 0 means non-stop only.
            airline_codes: Restrict to these airline codes, e.g. ["LY"].
            exclude_airline_codes: Exclude these airline codes.
            departure_time_min: Earliest departure hour, 0-23.
            departure_time_max: Latest departure hour, 0-23.
            arrival_time_min: Earliest arrival hour, 0-23.
            arrival_time_max: Latest arrival hour, 0-23.
            currency: ISO currency code, default "usd".
            max_price: Only return flights at or below this price.
            seat_type: 1 economy, 2 premium economy, 3 business, 4 first.
            passengers: Passenger counts as [adults, children, infants].
            sort_by: "best", "price", or "duration". Applied across all results.
            limit: Maximum flights to return, after merging and sorting.
            max_searches: Cap the billed requests this call may make. Lower it
                to spend less of the plan's quota on a wide search; the range
                is then sampled evenly rather than cut short.
            use_fallback: See USE_FALLBACK_DESCRIPTION. That text, not this
                line, is what the model actually sees -- see the note there.
        """
        if sort_by not in SORT_CHOICES:
            raise ToolError(f"sort_by must be one of {', '.join(SORT_CHOICES)}")

        bad_codes = invalid_airports(from_airport, to_airport)
        if bad_codes:
            raise ToolError(
                "Not valid airport codes: "
                + ", ".join(bad_codes)
                + ". Use three-letter IATA codes, e.g. TLV or JFK. "
                "The upstream answers an unusable code with an empty result, "
                "which reads as 'no flights on this route' -- so this is "
                "rejected here instead, and nothing is billed."
            )

        def plan_builder(cap: int):
            return plan_oneway(
                from_airport=from_airport,
                to_airport=to_airport,
                departure_date=departure_date,
                departure_date_from=departure_date_from,
                departure_date_to=departure_date_to,
                cap=cap,
            )

        def payload_builder(combo: dict[str, str]) -> dict[str, Any]:
            return build_oneway_payload(
                departure_date=combo["departure_date"],
                from_airport=normalise_origin(from_airport),
                to_airport=combo["to_airport"],
                max_stops=max_stops,
                airline_codes=airline_codes,
                exclude_airline_codes=exclude_airline_codes,
                departure_time_min=departure_time_min,
                departure_time_max=departure_time_max,
                arrival_time_min=arrival_time_min,
                arrival_time_max=arrival_time_max,
                currency=currency,
                max_price=max_price,
                seat_type=seat_type,
                passengers=passengers,
                limit=settings.default_result_limit,
                use_fallback=use_fallback,
            )

        return await _run(
            "search_oneway_flights",
            plan_builder,
            payload_builder,
            sort_by,
            limit,
            max_searches,
        )

    @mcp.tool(
        name="search_roundtrip_flights",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=FLIGHTS_OUTPUT_SCHEMA,
        title="FlightPowers: search round-trip flights",
        annotations=ToolAnnotations(
            title="FlightPowers: search round-trip flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described_flights(
            "FlightPowers round-trip fare search: live prices read from Google "
            "Flights, priced as paired legs rather than two separate one-ways. "
            "Input: origin and "
            "destination IATA codes -- the destination may be several codes, as "
            "\"BCN,LIS,ATH\" or [\"BCN\",\"LIS\",\"ATH\"] -- a departure "
            "date or range, and either a return date or a trip length in "
            "nights. Returns the total price for both legs, per-leg airline, "
            "stops and duration, and a single bookable buy_link for the trip.\n\n"
            "Use it for any return-trip fare question. For a flexible search "
            "make ONE call: pass departure_date_from / departure_date_to for "
            "the outbound range and `nights` instead of return_date to compare "
            "trip lengths -- '5 to 7 nights in Rome sometime in May' is one "
            "call.\n\n"
            "Each date/destination combination is one billed request; the "
            "count and the plan's remaining quota come back in `api_usage`.\n\n"
            "`by_destination` carries one entry per destination you asked for "
            "-- empty ones included, each with a `reason` -- so read it before "
            "telling a user a destination has no flights."
        ),
    )
    @document_params
    async def search_roundtrip_flights(
        from_airport: str,
        to_airport: str | list[str],
        departure_date: str | None = None,
        departure_date_from: str | None = None,
        departure_date_to: str | None = None,
        return_date: str | None = None,
        nights: int | list[int] | None = None,
        max_departure_stops: int | None = None,
        max_return_stops: int | None = None,
        departure_airline_codes: list[str] | None = None,
        return_airline_codes: list[str] | None = None,
        currency: str = "usd",
        max_price: int | None = None,
        seat_type: int | None = None,
        passengers: list[int] | None = None,
        sort_by: str = "best",
        limit: int = 10,
        max_searches: int | None = None,
        # Tri-state, matching the backend's own field. None is the default and
        # is dropped from the payload by `_compact`, which is what lets the
        # backend escalate to the fallback client as a last resort. An explicit
        # False would opt our own users out of that; an explicit True runs the
        # fallback inline on every attempt, which is the path that hangs.
        use_fallback: Annotated[
            bool | None, Field(description=USE_FALLBACK_DESCRIPTION)
        ] = None,
    ) -> dict[str, Any] | ToolResult:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV". One origin per
                search; a second one is refused rather than searched.
            to_airport: Destination airport. One IATA code ("BCN"), several
                separated by commas ("BCN,LIS,ATH"), or a list
                (["BCN","LIS","ATH"]) -- every shape is accepted and the
                destinations are compared in the same search.
            departure_date: Single outbound date, "YYYY-MM-DD".
            departure_date_from: First date of an outbound range.
            departure_date_to: Last date of an outbound range.
            return_date: Fixed return date. Use this OR nights, not both.
            nights: Trip length in nights; a number, or a list like [5, 6, 7].
                The return date is derived from each departure date.
            max_departure_stops: Maximum stops on the outbound leg.
            max_return_stops: Maximum stops on the return leg.
            departure_airline_codes: Restrict the outbound leg to these airlines.
            return_airline_codes: Restrict the return leg to these airlines.
            currency: ISO currency code, default "usd".
            max_price: Only return trips at or below this total price.
            seat_type: 1 economy, 2 premium economy, 3 business, 4 first.
            passengers: Passenger counts as [adults, children, infants].
            sort_by: "best", "price", or "duration". Applied across all results.
            limit: Maximum trips to return, after merging and sorting.
            max_searches: Cap the billed requests this call may make. Lower it
                to spend less of the plan's quota on a wide search; the range
                is then sampled evenly rather than cut short.
            use_fallback: See USE_FALLBACK_DESCRIPTION. That text, not this
                line, is what the model actually sees -- see the note there.
        """
        if sort_by not in SORT_CHOICES:
            raise ToolError(f"sort_by must be one of {', '.join(SORT_CHOICES)}")

        bad_codes = invalid_airports(from_airport, to_airport)
        if bad_codes:
            raise ToolError(
                "Not valid airport codes: "
                + ", ".join(bad_codes)
                + ". Use three-letter IATA codes, e.g. TLV or JFK. "
                "The upstream answers an unusable code with an empty result, "
                "which reads as 'no flights on this route' -- so this is "
                "rejected here instead, and nothing is billed."
            )

        def plan_builder(cap: int):
            return plan_roundtrip(
                from_airport=from_airport,
                to_airport=to_airport,
                departure_date=departure_date,
                departure_date_from=departure_date_from,
                departure_date_to=departure_date_to,
                return_date=return_date,
                nights=nights,
                cap=cap,
            )

        def payload_builder(combo: dict[str, str]) -> dict[str, Any]:
            return build_roundtrip_payload(
                departure_date=combo["departure_date"],
                return_date=combo["return_date"],
                from_airport=normalise_origin(from_airport),
                to_airport=combo["to_airport"],
                max_departure_stops=max_departure_stops,
                max_return_stops=max_return_stops,
                departure_airline_codes=departure_airline_codes,
                return_airline_codes=return_airline_codes,
                currency=currency,
                max_price=max_price,
                seat_type=seat_type,
                passengers=passengers,
                limit=settings.default_result_limit,
                use_fallback=use_fallback,
            )

        return await _run(
            "search_roundtrip_flights",
            plan_builder,
            payload_builder,
            sort_by,
            limit,
            max_searches,
        )

    # ── hotels ───────────────────────────────────────────────────────────
    # Same key, same gateway, different product. RapidAPI scopes access per
    # subscription, so a caller who has only bought flights gets a clean 403
    # here and a caller who has bought both gets everything -- which is why
    # one server can carry both without a second credential.

    async def _hotels_call(
        endpoint: str,
        payload: dict[str, Any],
        *,
        tool: str,
    ) -> dict[str, Any]:
        """Shared body for the hotel tools: resolve key, call, shape result."""
        started = time.perf_counter()
        headers, params = _request_context()
        credential: Credential = await _resolve(headers, params)

        if not credential.present:
            if credential.source in STORE_MISS_SOURCES:
                return _reconnect_message(
                    hotels_signup, upstream_api_name("hotels"), credential.source
                )
            return {
                "needs_api_key": True,
                "results": [],
                "result_count": 0,
                "signup_url": hotels_signup,
                "message": missing_key_message(
                    hotels_signup, upstream_api_name("hotels")
                ),
                "how_to_get_a_key": _howto(
                    hotels_signup, upstream_api_name("hotels")
                ),
            }

        quota: dict[str, int] = {}
        async with HotelsClient(
            timeout_seconds=settings.request_timeout_seconds,
            client=get_shared_client(settings),
        ) as client:
            try:
                body = await client.call(
                    endpoint, payload, api_key=credential.key, quota_sink=quota
                )
            except AuthError as exc:
                # Same shape as the flights `needs_api_key` reply, not a
                # raised ToolError: the model has to relay the fix to a
                # human, and a structured result survives that trip more
                # reliably than an exception string (see the flights no-key
                # branch above, which this mirrors -- a rejected/unsubscribed
                # key is functionally the same problem as no key at all).
                return {
                    "needs_api_key": True,
                    "results": [],
                    "result_count": 0,
                    "signup_url": hotels_signup,
                    "message": (
                        f"{exc} Subscribe to the "
                        f"{upstream_api_name('hotels')} at {hotels_signup} -- a "
                        "flights-only subscription does not cover hotel search."
                    ),
                    "how_to_get_a_key": _howto(
                        hotels_signup, upstream_api_name("hotels")
                    ),
                }
            except QuotaError as exc:
                raise ToolError(str(exc)) from exc
            except RapidAPIError as exc:
                raise ToolError(str(exc)) from exc

        # The hotels API answers `/search` with an object carrying
        # `properties`, and `/hotel_by_name` with a single property. Normalise
        # to a list so a model does not have to branch on the shape.
        if isinstance(body, dict):
            rows = body.get("properties")
            if rows is None:
                rows = [body]
        elif isinstance(body, list):
            rows = body
        else:
            rows = []

        logger.info(
            "tool=%s duration_ms=%d results=%d key_source=%s",
            tool,
            int((time.perf_counter() - started) * 1000),
            len(rows),
            credential.source,
        )

        result: dict[str, Any] = {
            "results": rows,
            "result_count": len(rows),
            "api_usage": _usage_block(1, quota, BILLING_UNIT_NOTES["hotels"]),
        }
        if isinstance(body, dict) and body.get("applied_filters"):
            result["applied_filters"] = body["applied_filters"]
        return result

    @mcp.tool(
        name="search_hotels",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=HOTELS_OUTPUT_SCHEMA,
        title="FlightPowers: search hotels",
        annotations=ToolAnnotations(
            title="FlightPowers: search hotels",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described_hotels(
            "FlightPowers hotel search: live Booking.com availability and "
            "nightly prices for a "
            "destination and date range. Input: a free-text destination the "
            "way a person would say it (\"Rome\", \"Tokyo Shibuya\"), plus "
            "check-in and check-out dates. Returns each property's price, "
            "review score, room type, location and a booking link.\n\n"
            "Set price_as_seen_from to a two-letter country code to price the "
            "same stay the way a shopper resident in that country would see "
            "it, which no other travel tool here can do. Gaps are real but "
            "usually modest and property-dependent, and rates move between "
            "calls, so hold one named property fixed, call each country a few "
            "times, and never read one call per country as a gap.\n\n"
            "Rates go stale within minutes: never reuse an earlier result, "
            "search again."
        ),
    )
    @document_params
    async def search_hotels(
        destination: str,
        checkin_date: str,
        checkout_date: str,
        adults: int | None = None,
        children: int | None = None,
        currency: str | None = None,
        budget_per_night: int | None = None,
        price_as_seen_from: str | None = None,
        filters: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            destination: Where to stay, in free text the way a person would
                say it, e.g. "Rome" or "Tokyo Shibuya". A city, district,
                landmark or region all work; no internal location ID is needed.
            checkin_date: First night of the stay, "YYYY-MM-DD".
            checkout_date: Departure morning, "YYYY-MM-DD". Must be after
                checkin_date.
            adults: Number of adult guests. Defaults to the upstream default
                when omitted.
            children: Number of children sharing the room.
            currency: ISO currency code for the prices returned, e.g. "usd".
            budget_per_night: Only return properties at or below this nightly
                price, in `currency`.
            price_as_seen_from: Two-letter country code, e.g. "de". Prices the
                stay through a residential connection in that country, so the
                result is what a shopper resident there would be quoted. For a
                rate-parity check hold one named property fixed and call each
                country a few times, because rates move between calls and one
                call per country can show a gap that is not there. Omit it for
                a neutral price.
            filters: Property filters to apply, e.g. ["free_cancellation",
                "breakfast_included"]. An unknown name is rejected with the
                list of valid ones rather than being ignored.
        """
        bad = unknown_filters(filters)
        if bad:
            raise ToolError(
                f"Unknown filter(s): {', '.join(bad)}. Valid filters are: "
                f"{', '.join(sorted(VALID_FILTERS))}"
            )
        payload = build_search_payload(
            destination=destination,
            checkin_date=checkin_date,
            checkout_date=checkout_date,
            adults=adults,
            children=children,
            currency=currency,
            budget_per_night=budget_per_night,
            proxy_country=price_as_seen_from,
            filters=filters,
        )
        return await _hotels_call("search", payload, tool="search_hotels")

    @mcp.tool(
        name="find_hotel_by_name",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=HOTELS_OUTPUT_SCHEMA,
        title="FlightPowers: find one hotel by name",
        annotations=ToolAnnotations(
            title="FlightPowers: find one hotel by name",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described_hotels(
            "FlightPowers single-property lookup: live Booking.com "
            "availability and pricing for one named property. Input: the "
            "hotel name a person would type (adding the city helps when a "
            "chain has many properties) plus check-in and check-out dates -- "
            "no internal property ID needed, the resolution is done for you. "
            "Returns the property's price, review score, room type and a "
            "booking link. Use it to check one specific hotel, or to track a "
            "single property's price over time.\n\n"
            "price_as_seen_from prices the stay as a shopper resident in that "
            "country would see it. Gaps are real but usually modest and "
            "property-dependent, and rates move between calls, so call each "
            "country a few times on this same property before reporting a "
            "gap.\n\n"
            "Rates go stale within minutes: never reuse an earlier result."
        ),
    )
    @document_params
    async def find_hotel_by_name(
        hotel_name: str,
        checkin_date: str,
        checkout_date: str,
        adults: int | None = None,
        children: int | None = None,
        currency: str | None = None,
        price_as_seen_from: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            hotel_name: The property name a person would type, e.g. "Hotel
                Artemide". Adding the city ("Hotel Artemide Rome") disambiguates
                a chain with many properties. No internal property ID is needed.
            checkin_date: First night of the stay, "YYYY-MM-DD".
            checkout_date: Departure morning, "YYYY-MM-DD". Must be after
                checkin_date.
            adults: Number of adult guests.
            children: Number of children sharing the room.
            currency: ISO currency code for the prices returned, e.g. "usd".
            price_as_seen_from: Two-letter country code, e.g. "de". Prices the
                stay as a shopper resident in that country would see it. Call
                each country a few times on this same property before
                reporting a gap, because rates move between calls and gaps are
                usually modest and property-dependent.
        """
        payload = build_hotel_by_name_payload(
            hotel_name=hotel_name,
            checkin_date=checkin_date,
            checkout_date=checkout_date,
            adults=adults,
            children=children,
            currency=currency,
            proxy_country=price_as_seen_from,
        )
        return await _hotels_call(
            "hotel_by_name", payload, tool="find_hotel_by_name"
        )

    # ── product selection ────────────────────────────────────────────────
    # Everything above registers unconditionally; this prunes down to what
    # this deployment sells. Registering-then-removing rather than wrapping
    # the definitions in a conditional keeps one code path for "both" and
    # avoids two near-identical blocks drifting apart.
    FLIGHT_TOOLS = ("search_oneway_flights", "search_roundtrip_flights")
    HOTEL_TOOLS = ("search_hotels", "find_hotel_by_name")

    if settings.products == "flights":
        drop = HOTEL_TOOLS
    elif settings.products == "hotels":
        drop = FLIGHT_TOOLS
    else:
        drop = ()

    for tool_name in drop:
        # mcp.remove_tool() is deprecated in fastmcp 3.4 in favour of the
        # local provider; using the deprecated alias emits a warning on every
        # cold start and will break on a future upgrade.
        mcp.local_provider.remove_tool(tool_name)

    # ── prompts ──────────────────────────────────────────────────────────
    # Ready-made actions the USER picks in the client, as opposed to tools the
    # model calls. They carry the three rules a model otherwise infers badly:
    # one call with a range, judge the fare against the price band, and say
    # what the search cost.
    if settings.products in ("flights", "both"):
        # All four prompts are flight questions; on a hotels-only
        # deployment they would be dead entries in the client's UI.
        register_prompts(mcp)

    # ── operational routes ───────────────────────────────────────────────

    site = site_origin

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Public, unauthenticated, and cheap -- registries poll it, and a
        listing that points at a dead endpoint is worse than no listing."""
        return JSONResponse(
            {
                "status": "ok",
                "service": service_name(settings.products),
                "ads": False,
                "mcp_endpoint": settings.public_url,
                "signup_url": settings.signup_url,
                # Directory reviews check that a privacy policy, terms and a
                # support channel all resolve for an anonymous visitor.
                # Emitting them from the same process that serves them means
                # they cannot drift apart.
                "privacy_url": f"{site}/privacy",
                "terms_url": f"{site}/terms",
                "support_url": f"{site}/support",
                "contact_email": CONTACT_EMAIL,
                # Stated out loud because a fallback key left set in
                # production silently bills its owner for every anonymous
                # caller, and nothing else would ever surface it.
                "server_side_key_configured": bool(settings.fallback_rapidapi_key),
                # Whether /connect exists on this deployment. Ops needs one
                # place to see that the four env vars actually took effect:
                # the page 404ing is otherwise indistinguishable from a
                # routing problem, and a half-set configuration disables the
                # feature silently on purpose.
                "connect_enabled": connect is not None,
                # Day 2: whether the always-challenging OAuth endpoint is
                # live, and where it is. Same reason as connect_enabled --
                # `/mcp/oauth` 404ing is otherwise indistinguishable from a
                # routing problem, and a directory listing that points at a
                # dead endpoint is worse than no listing.
                "oauth_enabled": oauth is not None,
                "oauth_mcp_endpoint": (
                    f"{site}{MCP_OAUTH_PATH}" if oauth is not None else None
                ),
            }
        )

    @mcp.custom_route("/", methods=["GET"])
    async def index(_request: Request) -> Response:
        """The base URL a reviewer types before they read anything else.

        FastMCP mounts nothing at `/`, so without this the first page a human
        opens on the submitted domain is a bare 404 -- which reads as an
        abandoned deployment next to a listing that claims the service is
        live. Links out to the policies, support and health instead."""
        return HTMLResponse(
            index_html(settings.products, site, settings.signup_url)
        )

    @mcp.custom_route("/privacy", methods=["GET"])
    async def privacy(_request: Request) -> Response:
        """Required by every app-directory submission, and rejected if it does
        not resolve for an anonymous visitor. Served here rather than on
        flightpowers.com because this is the deployment the policy describes."""
        body = render_document("privacy", settings.products)
        if body is None:
            return PlainTextResponse("policy unavailable", status_code=404)
        return HTMLResponse(body)

    @mcp.custom_route("/terms", methods=["GET"])
    async def terms(_request: Request) -> Response:
        body = render_document("terms", settings.products)
        if body is None:
            return PlainTextResponse("terms unavailable", status_code=404)
        return HTMLResponse(body)

    @mcp.custom_route("/support", methods=["GET"])
    async def support(_request: Request) -> Response:
        """Both platforms require reachable support details."""
        return HTMLResponse(support_html(settings.products))

    # ── /connect ─────────────────────────────────────────────────────────
    # Registered only when the feature is configured. An unconfigured
    # deployment 404s these paths exactly as it does today, which is the
    # honest answer: there is nothing behind them.
    if connect is not None:
        _cookie_secure = site_origin.lower().startswith("https://")

        def _set_cookie(
            response: Response, name: str, value: str, max_age: int
        ) -> None:
            """HttpOnly, SameSite=Lax, scoped to /connect.

            Lax rather than Strict because the sign-in ends in a top-level
            GET navigation back from Google, and Strict would drop the state
            cookie on exactly that hop -- the flow would fail for every user
            with an error that looks like a Google misconfiguration.
            Path=/connect so the cookie is never attached to a /mcp request:
            a session cookie is not a credential this server accepts there,
            and the cheapest way to prove that is for it not to arrive.
            """
            response.set_cookie(
                name,
                value,
                max_age=max_age,
                httponly=True,
                secure=_cookie_secure,
                samesite="lax",
                path=COOKIE_PATH,
            )

        _canonical_host = site_origin.split("://", 1)[-1].rstrip("/").lower()

        def _wrong_host(request: Request) -> Response | None:
            """Send an alias to the canonical origin before anything is set.

            `flights.flightpowers.com` and `google-flights-mcp.flightpowers.com`
            are the same deployment, but they are different COOKIE origins and
            Google compares `redirect_uri` literally. A sign-in started on the
            alias would set its state cookie on the alias, come back to the
            canonical host, find no cookie, and fail with a message that
            reads like a Google misconfiguration. One 302 up front costs a
            round trip and removes a whole class of support thread -- and it
            is why only ONE redirect URI per product has to be registered.
            """
            host = (request.headers.get("host") or "").strip().lower()
            if not host or host == _canonical_host:
                return None
            return RedirectResponse(f"{site_origin}/connect", status_code=302)

        @mcp.custom_route("/connect", methods=["GET"])
        async def connect_page(request: Request) -> Response:
            redirect = _wrong_host(request)
            if redirect is not None:
                return redirect
            identity = connect.auth.read_session(request.cookies.get(SESSION_COOKIE))
            if identity is None:
                return HTMLResponse(signed_out_html(settings.products))
            try:
                summary = await connect.store.summary(identity.sub)
            except KeyStoreError as exc:
                logger.warning("connect page could not read the store: %s", exc)
                return HTMLResponse(
                    signed_out_html(
                        settings.products,
                        banner=(
                            "The key store is not reachable right now. Nothing "
                            "was changed; try again in a minute."
                        ),
                    ),
                    status_code=503,
                )
            return HTMLResponse(
                signed_in_html(
                    email=identity.email,
                    product=settings.products,
                    mcp_url=settings.public_url,
                    summary=summary,
                    token=(
                        connect.auth.issue_connect_token(identity.sub)
                        if summary is not None
                        else None
                    ),
                    csrf=connect.csrf(identity.sub),
                )
            )

        @mcp.custom_route("/connect/start", methods=["GET"])
        async def connect_start(request: Request) -> Response:
            redirect = _wrong_host(request)
            if redirect is not None:
                return redirect
            # `next` is where to land after Google. It is validated by
            # `GoogleWebAuth.start` and then carried INSIDE the signed state
            # cookie, so what comes back at the callback cannot have been
            # edited into an open redirect. Used by /connect/authorize, which
            # sends a not-yet-signed-in user through this sign-in and wants
            # them returned to the pending authorization request rather than
            # to a page that has forgotten it.
            url, state_cookie = connect.auth.start(
                request.query_params.get("next", "")
            )
            response = RedirectResponse(url, status_code=302)
            _set_cookie(response, OAUTH_COOKIE, state_cookie, OAUTH_STATE_TTL_SECONDS)
            return response

        @mcp.custom_route("/connect/callback", methods=["GET"])
        async def connect_callback(request: Request) -> Response:
            error = request.query_params.get("error")
            if error:
                # The user pressed Cancel on Google's consent screen, most
                # of the time. Not an error page: send them back to the
                # start with a sentence, not a stack trace.
                return HTMLResponse(
                    signed_out_html(
                        settings.products,
                        banner="Google sign-in was cancelled. Nothing was changed.",
                    )
                )
            code = request.query_params.get("code", "")
            state = request.query_params.get("state", "")
            state_cookie = request.cookies.get(OAUTH_COOKIE, "")
            if not code or not state or not state_cookie:
                return HTMLResponse(
                    signed_out_html(
                        settings.products,
                        banner=(
                            "That sign-in link was incomplete or had expired. "
                            "Start again."
                        ),
                    ),
                    status_code=400,
                )
            try:
                identity = await connect.auth.finish(
                    code, state, state_cookie, client=get_shared_client(settings)
                )
            except WebAuthError as exc:
                logger.info("sign-in rejected: %s", exc)
                return HTMLResponse(
                    signed_out_html(
                        settings.products,
                        banner="That sign-in could not be completed. Start again.",
                    ),
                    status_code=400,
                )
            response = RedirectResponse(
                connect.auth.next_from_state(state_cookie) or "/connect",
                status_code=303,
            )
            _set_cookie(
                response,
                SESSION_COOKIE,
                connect.auth.issue_session(identity),
                SESSION_TTL_SECONDS,
            )
            response.delete_cookie(OAUTH_COOKIE, path=COOKIE_PATH)
            return response

        async def _render_signed_in(
            identity, *, notice: str = "", error: str = ""
        ) -> Response:
            try:
                summary = await connect.store.summary(identity.sub)
            except KeyStoreError as exc:
                # Reached when the store goes away between the write and the
                # render. The page still has to come back with a sentence
                # rather than a 500, because the user has just pressed a
                # button and needs to know whether it worked.
                logger.warning("could not read the store while rendering: %s", exc)
                summary = None
                error = error or (
                    "The key store is not reachable right now, so this page "
                    "may not reflect your latest change."
                )
            return HTMLResponse(
                signed_in_html(
                    email=identity.email,
                    product=settings.products,
                    mcp_url=settings.public_url,
                    summary=summary,
                    token=(
                        connect.auth.issue_connect_token(identity.sub)
                        if summary is not None
                        else None
                    ),
                    csrf=connect.csrf(identity.sub),
                    notice=notice,
                    error=error,
                )
            )

        @mcp.custom_route("/connect/save", methods=["POST"])
        async def connect_save(request: Request) -> Response:
            identity = connect.auth.read_session(request.cookies.get(SESSION_COOKIE))
            if identity is None:
                return RedirectResponse("/connect", status_code=303)
            form = await request.form()
            if not connect.csrf_ok(identity.sub, str(form.get("csrf", ""))):
                return await _render_signed_in(
                    identity,
                    error="That form had expired. Nothing was saved -- try again.",
                )
            key = str(form.get("rapidapi_key", "")).strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in {'"', "'"}:
                key = key[1:-1].strip()
            if not key:
                return await _render_signed_in(
                    identity, error="Paste a key before pressing Save."
                )
            if key_looks_malformed(key):
                return await _render_signed_in(
                    identity,
                    error=(
                        "That does not look like a RapidAPI key -- they are "
                        "around 50 characters. Nothing was saved and no "
                        "request was spent."
                    ),
                )
            if connect.validate:
                check = await check_rapidapi_key(
                    key, settings.products, client=get_shared_client(settings)
                )
                if not check.ok:
                    return await _render_signed_in(identity, error=check.message)
            else:
                check = None
            try:
                await connect.store.put(identity.sub, identity.email, key)
            except KeyStoreError as exc:
                logger.warning("could not store a key: %s", exc)
                return await _render_signed_in(
                    identity,
                    error=(
                        "The key could not be saved just now. Nothing was "
                        "stored; try again in a minute."
                    ),
                )
            # NEVER log the key, and never echo it back. The only trace this
            # leaves is the account it belongs to.
            logger.info("stored a key for sub=%s", identity.sub)
            notice = (
                check.message
                if check is not None and check.message
                else "Key saved. Copy the connect URL below into your MCP client."
            )
            return await _render_signed_in(identity, notice=notice)

        @mcp.custom_route("/connect/disconnect", methods=["POST"])
        async def connect_disconnect(request: Request) -> Response:
            identity = connect.auth.read_session(request.cookies.get(SESSION_COOKIE))
            if identity is None:
                return RedirectResponse("/connect", status_code=303)
            form = await request.form()
            if not connect.csrf_ok(identity.sub, str(form.get("csrf", ""))):
                return await _render_signed_in(
                    identity,
                    error="That form had expired. Nothing was changed -- try again.",
                )
            try:
                removed = await connect.store.revoke(identity.sub)
            except KeyStoreError as exc:
                logger.warning("could not revoke a key: %s", exc)
                return await _render_signed_in(
                    identity,
                    error=(
                        "The key could not be removed just now. Nothing was "
                        "changed; try again in a minute."
                    ),
                )
            # Disconnect has to mean disconnect. The key is gone, so every
            # OAuth token for this account now resolves to nothing anyway --
            # but leaving live tokens behind would mean a client that still
            # says "connected" while nothing works, and the consent page
            # promised otherwise. Best effort: a store outage here must not
            # turn a successful key removal into an error.
            if oauth is not None:
                try:
                    dropped = await oauth.store.revoke_for_user(
                        identity.sub, PROVIDER_GOOGLE
                    )
                    if dropped:
                        logger.info(
                            "revoked %d OAuth token(s) for sub=%s",
                            dropped,
                            identity.sub,
                        )
                except OAuthStoreError as exc:
                    logger.warning("could not revoke OAuth tokens: %s", exc)
            return await _render_signed_in(
                identity,
                notice=(
                    "Key removed. Every connect URL for this account has "
                    "stopped working."
                    if removed
                    else "There was no stored key to remove."
                ),
            )

    # ── MCP-protocol OAuth routes ────────────────────────────────────────
    # Registered only when the feature is configured, for the same reason
    # /connect's are: an unconfigured deployment answering 404 here is the
    # honest answer, and it is what makes this whole change a no-op until
    # ops sets the env vars.
    if oauth is not None:
        register_oauth_routes(mcp, oauth, settings, connect)

    # Read by src/entrypoint.py, which installs the /mcp/oauth ASGI gate.
    # An attribute rather than a second return value because `build_server`
    # returns a FastMCP everywhere -- tests, `python -m src`, both product
    # apps -- and widening that signature for one optional feature would
    # touch every one of those call sites.
    mcp.fp_oauth = oauth

    @mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
    async def openai_challenge(_request: Request) -> Response:
        """Domain verification for OpenAI's plugin/app submission.

        The reviewer's checker requires the response body to be the token and
        nothing else -- no JSON envelope, no trailing newline, no list. A 404
        until OPENAI_APPS_CHALLENGE_TOKEN is set, so an unconfigured server
        cannot appear to pass verification with an empty string.
        """
        token = os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN", "").strip()
        if not token:
            return PlainTextResponse("not configured", status_code=404)
        return PlainTextResponse(token, media_type="text/plain")

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(request: Request) -> JSONResponse:
        token = os.environ.get("METRICS_TOKEN", "")
        if token and request.headers.get("x-metrics-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        snapshot = await telemetry.snapshot()
        snapshot["config"] = {
            "service": service_name(settings.products),
            "ads_enabled": False,
            "max_searches_per_tool_call": settings.max_searches_per_tool_call,
            "max_concurrent_searches": settings.max_concurrent_searches,
            "rapidapi_host": settings.rapidapi_host,
            "server_side_key_configured": bool(settings.fallback_rapidapi_key),
            "public_url": settings.public_url,
        }
        return JSONResponse(snapshot)

    @mcp.custom_route("/metrics/calls", methods=["GET"])
    async def metrics_calls(request: Request) -> JSONResponse:
        """Call counts per UTC hour: GET /metrics/calls?hours=24"""
        token = os.environ.get("METRICS_TOKEN", "")
        if token and request.headers.get("x-metrics-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw = request.query_params.get("hours", "24")
        try:
            hours = int(raw)
        except ValueError:
            return JSONResponse(
                {"error": f"hours must be an integer, got {raw!r}"}, status_code=400
            )
        if hours < 1:
            return JSONResponse({"error": "hours must be >= 1"}, status_code=400)
        return JSONResponse(await telemetry.call_series(hours))

    mcp.telemetry = telemetry  # type: ignore[attr-defined]
    mcp.settings_obj = settings  # type: ignore[attr-defined]
    return mcp
