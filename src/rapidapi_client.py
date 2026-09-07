"""
Async client for the Google Flights Live API on RapidAPI.

Same two endpoints and the same request bodies as the free server's
LambdaClient -- the difference is entirely in who pays and therefore in how
failures must be handled.

Three behaviours that differ from talking to the Lambda directly, and each one
is a bug if you copy the free server's client across unchanged:

1. **429 is not transient.** Against the Lambda a 429 is backpressure worth
   retrying. On RapidAPI it means the caller has exhausted their plan quota,
   and every retry is another billed request against a quota that is already
   spent. It is raised immediately, as a distinct error, so the caller can be
   told to upgrade rather than told "temporarily unavailable".

2. **401 and 403 are the user's problem, not ours.** A wrong key and an
   unsubscribed key are different fixes (get a key vs subscribe to this API),
   and RapidAPI distinguishes them, so we keep them distinct too.

3. **Every request is money.** Retries are bounded at two attempts rather than
   three, and only for genuine server-side faults, because the person paying
   for the extra attempt is not the person who chose to make it.

Unchanged from the Lambda contract: None values are omitted from the payload
rather than sent as null (`sort_type` is a strict enum and an explicit null is
a 422), and an empty result is `[]` with HTTP 200, never a 404.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Literal

import httpx

from .fanout import split_airport_codes
from .settings import DEFAULT_TIMEOUT_SECONDS

ENDPOINT_MAP = {
    "oneway": "/api/google_flights/oneway/v1",
    "roundtrip": "/api/google_flights/roundtrip/v1",
}

# Server-side faults only. 429 is deliberately absent -- see the docstring.
_RETRYABLE_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 2

# IATA airport and city codes are exactly three letters.
_IATA_CODE = re.compile(r"[A-Za-z]{3}")


class RapidAPIError(RuntimeError):
    """A search could not be completed."""


class AuthError(RapidAPIError):
    """The key was rejected: missing, wrong, or not subscribed to this API."""


class QuotaError(RapidAPIError):
    """The caller's plan quota or rate limit is exhausted."""


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so we never send an explicit null.

    `use_fallback` depends on this. The backend field is tri-state -- true runs
    the fallback client inline on every attempt, false forbids it outright, and
    an absent value lets the backend escalate to it once after every retry for a
    combination has failed. Omitting the key is therefore the only way to ask for
    the last-resort behaviour, and sending `false` (which is what these tools did
    before) opts the caller out of it.
    """
    return {k: v for k, v in payload.items() if v is not None}


def invalid_airports(*values: str | list[str] | None) -> list[str]:
    """Airport codes the upstream cannot act on.

    Checked before the request for the same reason as `unknown_filters` on the
    hotel side: the upstream answers a blank or malformed code with `200 []`
    rather than an error, so a bad code is indistinguishable from a real
    "no flights on this route". A model that passed an empty string then tells
    the user there are no flights, and nothing anywhere records that the search
    was never valid.

    An IATA airport or city code is exactly three letters. `""` is reported as
    `(empty)` so the message names something the caller can actually see.

    Each value is split with `split_airport_codes` first, so the three shapes a
    model might use for a destination list -- `["BCN","LIS"]`, `"BCN,LIS"`,
    `"BCN LIS"` -- are all checked code by code. Before that split lived here,
    `"BCN,LIS,ATH"` was rejected whole as a single 11-character "code" while
    the free server happily fanned the same string out.
    """
    bad: list[str] = []
    for value in values:
        if value is None:
            continue
        # Iterated element by element rather than splitting the whole list at
        # once, so an empty element in `["LCA", "", "ATH"]` is still named.
        candidates = list(value) if isinstance(value, (list, tuple)) else [value]
        for candidate in candidates:
            codes = split_airport_codes(candidate)
            if not codes:
                bad.append("(empty)")
                continue
            bad.extend(code for code in codes if not _IATA_CODE.fullmatch(code))
    return bad


def build_oneway_payload(
    *,
    departure_date: str,
    from_airport: str,
    to_airport: str,
    max_stops: int | None = None,
    airline_codes: list[str] | None = None,
    exclude_airline_codes: list[str] | None = None,
    departure_time_min: int | None = None,
    departure_time_max: int | None = None,
    arrival_time_min: int | None = None,
    arrival_time_max: int | None = None,
    currency: str | None = None,
    max_price: int | None = None,
    seat_type: int | None = None,
    passengers: list[int] | None = None,
    limit: int | None = None,
    use_fallback: bool | None = None,
    use_ext_proxy: bool | None = None,
) -> dict[str, Any]:
    """Mirrors the Oneway API body documented in RAPID_API_README.md."""
    return _compact(
        {
            "departure_date": departure_date,
            "from_airport": from_airport,
            "to_airport": to_airport,
            "max_stops": max_stops,
            "airline_codes": airline_codes,
            "exclude_airline_codes": exclude_airline_codes,
            "departure_time_min": departure_time_min,
            "departure_time_max": departure_time_max,
            "arrival_time_min": arrival_time_min,
            "arrival_time_max": arrival_time_max,
            "currency": currency,
            "max_price": max_price,
            "seat_type": seat_type,
            "passengers": passengers,
            "limit": limit,
            "use_fallback": use_fallback,
            "use_ext_proxy": use_ext_proxy,
        }
    )


def build_roundtrip_payload(
    *,
    departure_date: str,
    return_date: str,
    from_airport: str,
    to_airport: str,
    max_departure_stops: int | None = None,
    max_return_stops: int | None = None,
    departure_airline_codes: list[str] | None = None,
    return_airline_codes: list[str] | None = None,
    departure_exclude_airline_codes: list[str] | None = None,
    return_exclude_airline_codes: list[str] | None = None,
    departure_departure_time_min: int | None = None,
    departure_departure_time_max: int | None = None,
    departure_arrival_time_min: int | None = None,
    departure_arrival_time_max: int | None = None,
    return_departure_time_min: int | None = None,
    return_departure_time_max: int | None = None,
    return_arrival_time_min: int | None = None,
    return_arrival_time_max: int | None = None,
    currency: str | None = None,
    max_price: int | None = None,
    seat_type: int | None = None,
    passengers: list[int] | None = None,
    limit: int | None = None,
    use_fallback: bool | None = None,
    use_ext_proxy: bool | None = None,
) -> dict[str, Any]:
    """Mirrors the Roundtrip API body documented in RAPID_API_README.md."""
    return _compact(
        {
            "departure_date": departure_date,
            "return_date": return_date,
            "from_airport": from_airport,
            "to_airport": to_airport,
            "max_departure_stops": max_departure_stops,
            "max_return_stops": max_return_stops,
            "departure_airline_codes": departure_airline_codes,
            "return_airline_codes": return_airline_codes,
            "departure_exclude_airline_codes": departure_exclude_airline_codes,
            "return_exclude_airline_codes": return_exclude_airline_codes,
            "departure_departure_time_min": departure_departure_time_min,
            "departure_departure_time_max": departure_departure_time_max,
            "departure_arrival_time_min": departure_arrival_time_min,
            "departure_arrival_time_max": departure_arrival_time_max,
            "return_departure_time_min": return_departure_time_min,
            "return_departure_time_max": return_departure_time_max,
            "return_arrival_time_min": return_arrival_time_min,
            "return_arrival_time_max": return_arrival_time_max,
            "currency": currency,
            "max_price": max_price,
            "seat_type": seat_type,
            "passengers": passengers,
            "limit": limit,
            "use_fallback": use_fallback,
            "use_ext_proxy": use_ext_proxy,
        }
    )


# RapidAPI's gateway reports the caller's plan usage on every response,
# including error responses. Reading them costs nothing and turns "you will
# find out on your invoice" into a number in the tool result.
QUOTA_HEADERS = {
    "x-ratelimit-requests-limit": "plan_requests_limit",
    "x-ratelimit-requests-remaining": "plan_requests_remaining",
    "x-ratelimit-requests-reset": "plan_seconds_until_reset",
}


# The backend reports the outcome of a search in headers under this prefix.
# `X-Search-Status: degraded` means the search did not complete, so the `[]` it
# came with says nothing about flight availability. Matched by prefix so a
# counter added upstream arrives here without an edit.
SEARCH_HEADER_PREFIX = "x-search-"
SEARCH_STATUS_HEADER = "x-search-status"
SEARCH_REASON_HEADER = "x-search-reason"

#: The search did not complete. An empty list carrying this is not an answer.
SEARCH_STATUS_DEGRADED = "degraded"
#: Some combinations answered and some did not; the list is incomplete.
SEARCH_STATUS_PARTIAL = "partial"

#: Statuses that mean "do not report this result as a fact about flights".
INCOMPLETE_SEARCH_STATUSES = frozenset(
    {SEARCH_STATUS_DEGRADED, SEARCH_STATUS_PARTIAL}
)


def read_search_status(response: httpx.Response) -> dict[str, str]:
    """Extract the backend's `X-Search-*` outcome headers, lower-cased.

    Absent headers produce an empty dict, which every caller reads as "the
    backend did not say" -- deliberately *not* as "the search was fine". A
    backend that predates these headers must not be assumed healthy.
    """
    return {
        name.lower(): str(value)
        for name, value in response.headers.items()
        if name.lower().startswith(SEARCH_HEADER_PREFIX)
    }


def search_is_incomplete(outcome: dict[str, str]) -> bool:
    """True when this response's result list is known not to be an answer."""
    return outcome.get(SEARCH_STATUS_HEADER, "") in INCOMPLETE_SEARCH_STATUSES


def read_quota(response: httpx.Response) -> dict[str, int]:
    """Extract plan usage from RapidAPI's rate-limit headers.

    Absent headers simply produce a smaller dict -- these are informational,
    and a gateway that stops sending them must not break a search.
    """
    quota: dict[str, int] = {}
    for header, field in QUOTA_HEADERS.items():
        raw = response.headers.get(header)
        if raw is None:
            continue
        try:
            quota[field] = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
    return quota


def _upstream_message(response: httpx.Response) -> str:
    """RapidAPI's own explanation, which is usually the useful one.

    Its gateway answers with {"message": "..."} on auth and quota failures and
    those messages already name the fix (subscribe, upgrade, check your key).
    Falls back to a truncated body when the shape is anything else.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:300].strip()
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(body)[:300]


class RapidAPIClient:
    """Thin async HTTP client over the two /v1 flight endpoints.

    The key is passed per call rather than held on the instance: one process
    serves many callers, each with their own subscription, and an instance
    attribute would be exactly the kind of state that leaks one user's key
    into another user's request under concurrency.
    """

    def __init__(
        self,
        base_url: str,
        rapidapi_host: str,
        # The deployed function's ``Timeout`` plus an edge-relay margin; see
        # ``settings.DEFAULT_TIMEOUT_SECONDS``. There is one ceiling here, not
        # two: measured on 2026-08-27 the RapidAPI edge relays the verdict about
        # a quarter-second after the function's own kill, so the number to clear
        # is the function ``Timeout``. Waiting *less* than it discards answers
        # that were on their way, which is what a stale 45.0 did after the
        # function was raised to 60.
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = rapidapi_host
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "RapidAPIClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        endpoint: Literal["oneway", "roundtrip"],
        payload: dict[str, Any],
        *,
        api_key: str,
        quota_sink: dict[str, int] | None = None,
        outcome_sink: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """POST one search. Returns the (possibly empty) result list.

        Raises AuthError, QuotaError, or RapidAPIError. Never returns None --
        an empty search is `[]`, which is a valid answer *only when the backend
        says the search completed*; see `outcome_sink`.

        `quota_sink`, when given, is overwritten in place with the plan usage
        read off the response. Every request in one fan-out carries the same
        key, so last-writer-wins is not a race to avoid but the behaviour we
        want: the final value is the most recent view of that plan's usage.

        `outcome_sink`, when given, gets one appended entry per answered
        request: that response's `X-Search-*` headers. A list rather than a
        dict, because unlike the quota this is *not* one fact about the
        request -- each date and destination combination has its own outcome,
        and last-writer-wins would hide a failed one behind a healthy one.
        """
        if self._client is None:
            raise RapidAPIError("RapidAPIClient used outside its async context")

        url = f"{self._base_url}{ENDPOINT_MAP[endpoint]}"
        headers = {
            "Content-Type": "application/json",
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": self._host,
        }

        last_error = "unknown"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == _MAX_ATTEMPTS:
                    break
                await self._backoff(attempt)
                continue

            if quota_sink is not None:
                quota_sink.update(read_quota(response))

            if response.status_code == 200:
                if outcome_sink is not None:
                    outcome_sink.append(read_search_status(response))
                return self._parse(response)

            detail = _upstream_message(response)

            if response.status_code in (401, 403):
                raise AuthError(detail)
            if response.status_code == 429:
                raise QuotaError(detail)

            last_error = f"HTTP {response.status_code}: {detail}"
            if response.status_code not in _RETRYABLE_STATUS:
                raise RapidAPIError(f"{endpoint} search failed -- {last_error}")
            if attempt == _MAX_ATTEMPTS:
                break
            await self._backoff(attempt)

        raise RapidAPIError(
            f"{endpoint} search failed after {_MAX_ATTEMPTS} attempts -- {last_error}"
        )

    @staticmethod
    def _parse(response: httpx.Response) -> list[dict[str, Any]]:
        try:
            data = response.json()
        except ValueError as exc:
            raise RapidAPIError(
                f"upstream returned non-JSON: {response.text[:200]}"
            ) from exc

        # The API returns a bare array. Be tolerant of an enveloped shape in
        # case that ever changes.
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "items", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
            return []
        return []

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Full jitter. The upstream fans out to Google behind a shared proxy
        # pool, so synchronised retries are the last thing it needs.
        delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
        await asyncio.sleep(random.uniform(0, delay))
