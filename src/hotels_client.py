"""
Async client for the Booking Live API on RapidAPI.

Deliberately reuses the flights client's error types, retry policy and quota
reader rather than restating them. The two APIs are different products but
they sit behind the same gateway, are billed to the same key, and fail in the
same ways -- so a caller who has learned what `QuotaError` means for flights
should not have to learn a second vocabulary for hotels.

The one thing genuinely specific to hotels is `proxy_country`. It prices a
stay the way a shopper resident in that country would see it, which is what
makes rate-parity and geo-pricing monitoring possible. It is the most
under-sold capability in the whole product, so it is surfaced as a first-class
argument rather than buried in a passthrough dict.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

import httpx

from .rapidapi_client import (
    AuthError,
    QuotaError,
    RapidAPIError,
    read_quota,
    _compact,
    _upstream_message,
)

HOTELS_HOST = "booking-live-api.p.rapidapi.com"

ENDPOINT_MAP = {
    "search": "/search",
    "hotel_by_name": "/hotel_by_name",
    "resolve": "/resolve",
}

# Server-side faults only. 429 is absent for the same reason as in the flights
# client: on RapidAPI it means the caller's plan is spent, and a retry bills
# them again for a request that cannot succeed.
_RETRYABLE_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 2

# The 24 filters the API accepts, matching the Booking.com UI. Kept here so an
# invalid value can be rejected before it costs the caller a request.
VALID_FILTERS = frozenset(
    {
        "free_cancellation",
        "breakfast_included",
        "breakfast_and_lunch",
        "breakfast_and_dinner",
        "all_meals_included",
        "all_inclusive",
        "free_wifi",
        "swimming_pool",
        "gym",
        "parking",
        "front_desk_24h",
        "review_score_7",
        "review_score_8",
        "review_score_9",
        "private_bathroom",
        "air_conditioning",
        "stars_3",
        "stars_4",
        "stars_5",
        "pets_allowed",
        "adults_only",
        "sauna",
        "very_good_breakfast",
        "accepts_online_payment",
    }
)


def build_search_payload(
    *,
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    currency: str | None = None,
    budget_per_night: int | None = None,
    proxy_country: str | None = None,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Body for `POST /search`. None values are dropped, never sent as null."""
    return _compact(
        {
            "destination": destination,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": currency,
            "budget_per_night": budget_per_night,
            "proxy_country": proxy_country,
            "filters": filters or None,
        }
    )


def build_hotel_by_name_payload(
    *,
    hotel_name: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    currency: str | None = None,
    proxy_country: str | None = None,
) -> dict[str, Any]:
    """Body for `POST /hotel_by_name`."""
    return _compact(
        {
            "hotel_name": hotel_name,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": currency,
            "proxy_country": proxy_country,
        }
    )


def unknown_filters(filters: list[str] | None) -> list[str]:
    """Filters the API does not accept.

    Checked before the request so a typo costs nothing. The API's behaviour
    for an unknown filter is not documented, and silently returning unfiltered
    results would be the worst outcome -- a caller monitoring "free
    cancellation only" would get everything and not know.
    """
    if not filters:
        return []
    return sorted({f for f in filters if f not in VALID_FILTERS})


class HotelsClient:
    """Thin async HTTP client over the hotel endpoints.

    As with the flights client, the key is passed per call rather than held on
    the instance: one process serves many callers with their own
    subscriptions, and instance state is how one user's key ends up on another
    user's request.
    """

    def __init__(
        self,
        base_url: str = f"https://{HOTELS_HOST}",
        rapidapi_host: str = HOTELS_HOST,
        timeout_seconds: float = 105.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = rapidapi_host
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "HotelsClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(
        self,
        endpoint: Literal["search", "hotel_by_name", "resolve"],
        payload: dict[str, Any],
        *,
        api_key: str,
        quota_sink: dict[str, int] | None = None,
    ) -> Any:
        """One request. Raises AuthError / QuotaError / RapidAPIError."""
        if self._client is None:
            raise RuntimeError("HotelsClient must be used as an async context manager")

        url = f"{self._base_url}{ENDPOINT_MAP[endpoint]}"
        headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": self._host,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except httpx.TimeoutException as exc:
                last_error = RapidAPIError(
                    "The hotels API did not respond in time. Retrying usually "
                    "succeeds; nothing was booked or charged."
                )
                if attempt == _MAX_ATTEMPTS:
                    raise last_error from exc
            else:
                if quota_sink is not None:
                    quota_sink.update(read_quota(response))

                if response.status_code in (401, 403):
                    raise AuthError(_upstream_message(response))
                if response.status_code == 429:
                    raise QuotaError(_upstream_message(response))
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError:
                        return response.text

                last_error = RapidAPIError(
                    f"[{response.status_code}] {_upstream_message(response)}"
                )
                if (
                    response.status_code not in _RETRYABLE_STATUS
                    or attempt == _MAX_ATTEMPTS
                ):
                    raise last_error

            await asyncio.sleep(0.25 * attempt + random.uniform(0, 0.25))

        assert last_error is not None
        raise last_error
