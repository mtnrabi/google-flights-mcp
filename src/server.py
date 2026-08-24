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
  search runs rather than post-sorting, it is silently dropped for one-way by
  api_lambda.py, and `max_price` overrides it. Results from up to `cap`
  searches are merged here anyway, so this server lets the backend default
  apply and sorts the merged set itself via `sort_by`. That is predictable;
  passing `sort_type` through is not -- the generated passthrough MCP exposes
  it on one-way today and it does nothing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from .credentials import (
    Credential,
    key_looks_malformed,
    missing_key_message,
    redact,
    resolve_credential,
)
from .legal import support_html, render_document
from .hotels_client import (
    VALID_FILTERS,
    HotelsClient,
    build_hotel_by_name_payload,
    build_search_payload,
    unknown_filters,
)
from .prompts import register_prompts
from .fanout import (
    FanoutResult,
    PlanError,
    execute_plan,
    plan_oneway,
    plan_roundtrip,
)
from .rapidapi_client import (
    AuthError,
    QuotaError,
    RapidAPIClient,
    RapidAPIError,
    build_oneway_payload,
    build_roundtrip_payload,
    invalid_airports,
)
from .settings import Settings, load_settings
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


def _sort_results(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    """Sort the merged result set.

    One-way and round-trip use different price/duration keys, so fall back
    across both. Missing values sort last rather than crashing -- the fli
    fallback path legitimately returns nulls.
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

    if sort_by == "duration":
        return sorted(rows, key=duration_key)
    return sorted(rows, key=price_key)


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats across combinations, keyed on buy_link.

    buy_link is already the de-dup key used elsewhere in this codebase
    (backend/src/app.py:533).
    """
    seen: set[str] = set()
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


def _usage_block(calls: int, quota: dict[str, int]) -> dict[str, Any]:
    """The `api_usage` field carried by every successful response.

    Deliberately verbose about what a "request" is. A user who believes one
    question costs one request, and then finds fifteen on their invoice, does
    not come back -- and the reason they were fifteen (a date range they asked
    for) is defensible only if it was stated at the time.
    """
    usage: dict[str, Any] = {"requests_used_by_this_call": calls}
    usage.update(quota)
    remaining = quota.get("plan_requests_remaining")
    limit = quota.get("plan_requests_limit")
    if remaining is not None and limit is not None:
        usage["note"] = (
            f"This search used {calls} of your RapidAPI plan's requests; "
            f"{remaining} of {limit} remain in the current period. "
            "Each date and destination combination is one billed request."
        )
    else:
        usage["note"] = (
            f"This search used {calls} of your RapidAPI plan's requests. "
            "Each date and destination combination is one billed request."
        )
    return usage


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or load_settings()

    mcp = FastMCP(
        name=service_name(settings.products),
        version="1.0.0",
        instructions=(
            "Real-time Google Flights search, ad-free. Requires the caller's "
            "own RapidAPI key for the Google Flights Live API, supplied as an "
            "`x-rapidapi-key` header or a `?rapidapi_key=` query parameter on "
            "the server URL; get one at "
            f"{settings.signup_url}\n\n"
            "Both tools accept a date RANGE and a LIST of destinations and "
            "expand them internally -- always express a flexible search as ONE "
            "call with a range, never as many single-date calls. Every "
            "combination is one request billed to the caller's plan, and the "
            "count is returned in `api_usage`."
        ),
    )

    http_client = get_shared_client(settings)
    telemetry = Telemetry(
        store=build_counter_store(client=http_client),
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

    # ── shared execution path ────────────────────────────────────────────

    async def _run(
        tool_name: str,
        plan_builder,
        payload_builder,
        sort_by: str,
        limit: int,
        max_searches: int | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        headers, params = _request_context()
        credential: Credential = resolve_credential(
            headers, params, fallback=settings.fallback_rapidapi_key
        )

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
                error="no_api_key",
            )
            return {
                "needs_api_key": True,
                "results": [],
                "result_count": 0,
                "signup_url": settings.signup_url,
                "message": missing_key_message(settings.signup_url),
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

        state = _UpstreamState()

        async def run_search(endpoint: str, payload: dict[str, Any]):
            try:
                return await client.search(
                    endpoint,
                    payload,
                    api_key=credential.key,
                    quota_sink=state.quota,
                )
            except AuthError as exc:
                state.auth_error = str(exc)
                raise
            except QuotaError as exc:
                state.quota_error = str(exc)
                raise

        # Reuses the process-wide connection pool; RapidAPIClient does not
        # close a client it was handed.
        async with RapidAPIClient(
            settings.rapidapi_base_url,
            settings.rapidapi_host,
            settings.request_timeout_seconds,
            client=http_client,
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
                "signup_url": settings.signup_url,
                "message": (
                    "The Google Flights API rejected this RapidAPI key: "
                    f"{state.auth_error}{hint}\n\n"
                    "Most often this means the key is valid but is not "
                    "subscribed to this specific API. Subscribing to the free "
                    f"tier at {settings.signup_url} fixes it."
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
                "signup_url": settings.signup_url,
                "api_usage": _usage_block(outcome.backend_calls_made, state.quota),
                "message": (
                    "This RapidAPI plan is out of requests for the current "
                    f"period: {state.quota_error}\n\n"
                    "A flexible search costs one request per date and "
                    "destination combination, so narrowing the range makes a "
                    "remaining quota go further. Plans can be changed at "
                    f"{settings.signup_url}."
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

        rows = _sort_results(_dedupe(outcome.results), sort_by)[:limit]

        await log(
            requested=plan.requested_combinations,
            calls=outcome.backend_calls_made,
            failures=outcome.backend_failures,
            results=len(rows),
            truncated=plan.truncated,
            error=None,
        )

        response: dict[str, Any] = {
            "results": rows,
            "result_count": len(rows),
            "search_coverage": plan.coverage(),
            "api_usage": _usage_block(outcome.backend_calls_made, state.quota),
        }
        if outcome.backend_failures:
            response["partial"] = (
                f"{outcome.backend_failures} of {plan.executed_combinations} "
                "searches failed; results cover the rest."
            )
        if not rows:
            response["message"] = (
                "No flights were found for this search. Google Flights returns "
                "nothing for some route and date combinations; try a different "
                "date, a nearby airport, or set use_fallback to true."
            )
        return response

    # ── tools ────────────────────────────────────────────────────────────

    @mcp.tool(
        name="search_oneway_flights",
        title="Search one-way flights",
        # Required by Anthropic's directory review, and a listed rejection
        # reason at OpenAI: a tool with no annotations is treated as
        # potentially destructive. Both tools here only read -- they cannot
        # book, hold, pay for or cancel anything -- and both reach a live
        # third-party API whose result set is not a closed domain, hence
        # openWorldHint. Not idempotent: fares change between identical calls.
        annotations=ToolAnnotations(
            title="Search one-way flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=(
            "Search real-time one-way flights on Google Flights. Input: origin "
            "and destination IATA codes (destination may be a list) plus either "
            "one departure date or a date range. Returns each flight's price, "
            "airline, duration, stops, a bookable buy_link, and Google's "
            "historical price range (price_insights_low / price_insights_high) "
            "so you can say whether a fare is actually a good deal.\n\n"
            "Use it for any one-way fare question, including open-ended ones. "
            "For a flexible search make ONE call with a date range and/or "
            "several destinations -- do NOT call it once per date. 'Cheapest "
            "flight to Sri Lanka anywhere in October' is one call, not thirty.\n\n"
            "Requires the caller's own RapidAPI key. Each date/destination "
            "combination is one billed request; the count and the plan's "
            "remaining quota come back in `api_usage`."
        ),
    )
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
        use_fallback: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, or a list of them to compare.
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
            use_fallback: Wait longer on hard routes. Slower, fewer empty results.
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
                from_airport=from_airport.strip().upper(),
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
        title="Search round-trip flights",
        annotations=ToolAnnotations(
            title="Search round-trip flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=(
            "Search real-time round-trip flights on Google Flights, priced as "
            "paired legs rather than two separate one-ways. Input: origin and "
            "destination IATA codes (destination may be a list), a departure "
            "date or range, and either a return date or a trip length in "
            "nights. Returns the total price for both legs, per-leg airline, "
            "stops and duration, and a single bookable buy_link for the trip.\n\n"
            "Use it for any return-trip fare question. For a flexible search "
            "make ONE call: pass departure_date_from / departure_date_to for "
            "the outbound range and `nights` instead of return_date to compare "
            "trip lengths -- '5 to 7 nights in Rome sometime in May' is one "
            "call.\n\n"
            "Requires the caller's own RapidAPI key. Each date/destination "
            "combination is one billed request; the count and the plan's "
            "remaining quota come back in `api_usage`."
        ),
    )
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
        use_fallback: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, or a list of them to compare.
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
            use_fallback: Wait longer on hard routes. Slower, fewer empty results.
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
                from_airport=from_airport.strip().upper(),
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
        credential: Credential = resolve_credential(
            headers, params, fallback=settings.fallback_rapidapi_key
        )

        if not credential.present:
            return {
                "needs_api_key": True,
                "results": [],
                "result_count": 0,
                "signup_url": settings.signup_url,
                "message": missing_key_message(settings.signup_url),
            }

        quota: dict[str, int] = {}
        async with HotelsClient(
            timeout_seconds=settings.request_timeout_seconds,
            client=http_client,
        ) as client:
            try:
                body = await client.call(
                    endpoint, payload, api_key=credential.key, quota_sink=quota
                )
            except AuthError as exc:
                raise ToolError(
                    f"{exc} Subscribe to the Booking Live API at "
                    f"{settings.signup_url} -- a flights-only subscription "
                    "does not cover hotel search."
                ) from exc
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
            "api_usage": _usage_block(1, quota),
        }
        if isinstance(body, dict) and body.get("applied_filters"):
            result["applied_filters"] = body["applied_filters"]
        return result

    @mcp.tool(
        name="search_hotels",
        title="Search hotels",
        annotations=ToolAnnotations(
            title="Search hotels",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=(
            "Search live hotel availability and nightly prices for a "
            "destination and date range. Input: a free-text destination the "
            "way a person would say it (\"Rome\", \"Tokyo Shibuya\"), plus "
            "check-in and check-out dates. Returns each property's price, "
            "review score, room type, location and a booking link.\n\n"
            "Set price_as_seen_from to a two-letter country code to price the "
            "same stay the way a shopper resident in that country would see "
            "it -- that is how rate-parity and geo-pricing differences are "
            "found, and no other travel tool here can do it.\n\n"
            "Rates go stale within minutes: never reuse an earlier result, "
            "search again."
        ),
    )
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
        title="Find one hotel by name",
        annotations=ToolAnnotations(
            title="Find one hotel by name",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=(
            "Get availability and pricing for one named property. Input: the "
            "hotel name a person would type (adding the city helps when a "
            "chain has many properties) plus check-in and check-out dates -- "
            "no internal property ID needed, the resolution is done for you. "
            "Returns the property's price, review score, room type and a "
            "booking link. Use it to check one specific hotel, or to track a "
            "single property's price over time.\n\n"
            "price_as_seen_from prices the stay as a shopper in that country "
            "would see it, which is what makes rate-parity checks possible.\n\n"
            "Rates go stale within minutes: never reuse an earlier result."
        ),
    )
    async def find_hotel_by_name(
        hotel_name: str,
        checkin_date: str,
        checkout_date: str,
        adults: int | None = None,
        children: int | None = None,
        currency: str | None = None,
        price_as_seen_from: str | None = None,
    ) -> dict[str, Any]:
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
                # Stated out loud because a fallback key left set in
                # production silently bills its owner for every anonymous
                # caller, and nothing else would ever surface it.
                "server_side_key_configured": bool(settings.fallback_rapidapi_key),
            }
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
