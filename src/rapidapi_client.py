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
    """Drop None values so we never send an explicit null."""
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
    """
    bad: list[str] = []
    for value in values:
        if value is None:
            continue
        candidates = value if isinstance(value, list) else [value]
        for code in candidates:
            text = (code or "").strip()
            if not _IATA_CODE.fullmatch(text):
                bad.append(text or "(empty)")
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
        timeout_seconds: float = 105.0,
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
    ) -> list[dict[str, Any]]:
        """POST one search. Returns the (possibly empty) result list.

        Raises AuthError, QuotaError, or RapidAPIError. Never returns None --
        an empty search is `[]`, which is a valid answer and not a failure.

        `quota_sink`, when given, is overwritten in place with the plan usage
        read off the response. Every request in one fan-out carries the same
        key, so last-writer-wins is not a race to avoid but the behaviour we
        want: the final value is the most recent view of that plan's usage.
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
