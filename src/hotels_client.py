"""
Async client for the Booking Live API on RapidAPI.

Deliberately reuses the flights client's error types, retry policy and quota
reader rather than restating them. The two APIs are different products but
they sit behind the same gateway, are billed to the same key, and fail in the
same ways -- so a caller who has learned what `QuotaError` means for flights
should not have to learn a second vocabulary for hotels.

The one thing genuinely specific to hotels is `proxy_country`. It prices a
stay the way a shopper resident in that country would see it, which is what
makes rate-parity and geo-pricing monitoring possible. Measured gaps are real
but modest, and repeat calls to the same country are not always identical, so
an honest comparison holds one named property fixed and takes several samples
per country. It is the most under-sold capability in the whole product, so it
is surfaced as a first-class argument rather than buried in a passthrough
dict.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import date, datetime, timezone
from typing import Any, Literal

import httpx

from .settings import DEFAULT_TIMEOUT_SECONDS

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
_TIMEOUT_MESSAGE = (
    "The hotels API did not respond in time. Retrying usually "
    "succeeds; nothing was booked or charged."
)

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
        # The deployed function's ``Timeout`` plus an edge-relay margin; see
        # ``settings.DEFAULT_TIMEOUT_SECONDS``. There is one ceiling here, not
        # two: measured on 2026-08-27 the RapidAPI edge relays the verdict about
        # a quarter-second after the function's own kill, so the number to clear
        # is the function ``Timeout``. Waiting *less* than it discards answers
        # that were on their way, which is what a stale 45.0 did after the
        # function was raised to 60.
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        # 1 = no retry (search_airbnb: the listing bills every request, and
        # its Lambda already retries a blocked page through the proxy).
        max_attempts: int = _MAX_ATTEMPTS,
        # What a timeout says. The default promises a free retry, which holds
        # for the Booking listing only; a listing called once says its own.
        timeout_message: str = _TIMEOUT_MESSAGE,
    ) -> None:
        self._max_attempts = max_attempts
        self._timeout_message = timeout_message
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
        attempt_sink: list[int] | None = None,
    ) -> Any:
        """One request. Raises AuthError / QuotaError / RapidAPIError.

        `attempt_sink` counts HTTP requests SENT, not calls made: a retry is
        billed by RapidAPI like any other request. Same contract as
        `RapidAPIClient.search`; see its docstring for why the cost is
        counted rather than assumed.
        """
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
        for attempt in range(1, self._max_attempts + 1):
            if attempt_sink is not None:
                attempt_sink.append(1)
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except httpx.TimeoutException as exc:
                last_error = RapidAPIError(self._timeout_message)
                if attempt == self._max_attempts:
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
                    or attempt == self._max_attempts
                ):
                    raise last_error

            await asyncio.sleep(0.25 * attempt + random.uniform(0, 0.25))

        assert last_error is not None
        raise last_error


# ── Airbnb Live API (search_airbnb, behind MCP_AIRBNB) ──────────────────────
# A separate RapidAPI listing with its own subscription, called on the
# caller's own key exactly like the Booking listing above, through the same
# HotelsClient (only the host differs). The Lambda behind it is otaLiteAgent;
# its POST /search vocabulary is hotel_agent nodejs_mvp/providers/airbnb.js
# (SEARCH_PARAMS, SORT_VALUES, ROOM_TYPES), which the listing forwards
# unchanged.

AIRBNB_HOST = "airbnb-live-api.p.rapidapi.com"
AIRBNB_SIGNUP_URL = "https://rapidapi.com/mtnrabi/api/airbnb-live-api"
AIRBNB_API_NAME = "Airbnb Live API"

#: airbnb.js SORT_VALUES. Airbnb has no ordering control; the Lambda sorts the
#: page it read and refuses any other value with a 400 (a billed request), so
#: the tool schema carries the enum and a bad value costs nothing.
AIRBNB_SORT_VALUES = ("price_asc", "price_desc", "rating_desc")

#: airbnb.js ROOM_TYPES, spelled as Airbnb's search UI spells them. The Lambda
#: drops an unknown value silently (a wider result), so the schema refuses it.
AIRBNB_ROOM_TYPES = ("Entire home/apt", "Private room", "Shared room", "Hotel room")

#: The Airbnb listing is called once (no retry) and RapidAPI meters every
#: request that reaches it, so a timeout promises neither a free retry nor
#: "not charged".
AIRBNB_TIMEOUT_MESSAGE = (
    "The Airbnb Live API did not respond in time, so nothing is known about "
    "these dates. Nothing was booked; RapidAPI may still count the request "
    "against your plan."
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strict_date(value: Any) -> date | None:
    """`YYYY-MM-DD` and a real calendar day, or None (hotel_agent
    date_fields.js parseISODate: `date.fromisoformat` alone also takes
    `20270305` and week dates)."""
    if not isinstance(value, str) or not _ISO_DATE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def airbnb_input_problem(
    *,
    checkin_date: str,
    checkout_date: str,
    counts: dict[str, int | None],
    min_rating: float | None,
    price_min: float | None,
    price_max: float | None,
    today: date | None = None,
) -> str | None:
    """Why this search would be refused, before it is sent; None if usable.

    Every request that reaches the listing is billed, refusals included, so
    what the Lambda would refuse (hotel_agent date_fields.js: a bad date, a
    check-in before today UTC, a checkout not after the check-in) is refused
    here for free. Only lower bounds on the counts: nothing in hotel_agent
    records Airbnb's ceilings, and a guessed one would refuse real searches.
    """
    checkin = _strict_date(checkin_date)
    checkout = _strict_date(checkout_date)
    bad = [
        name
        for name, parsed in (("checkin_date", checkin), ("checkout_date", checkout))
        if parsed is None
    ]
    if bad:
        return f"{' and '.join(bad)} must be a calendar date written YYYY-MM-DD."
    today = today or datetime.now(timezone.utc).date()
    if checkin < today:
        return f"checkin_date {checkin_date} is in the past (today is {today.isoformat()}, UTC)."
    if checkout <= checkin:
        return "checkout_date must be after checkin_date: a stay is at least one night."
    for name, value in counts.items():
        floor = 1 if name == "adults" else 0
        if value is not None and value < floor:
            return f"{name} must be {floor} or more."
    if min_rating is not None and not 0 <= min_rating <= 5:
        return "min_rating is out of 5: a number from 0 to 5."
    for name, value in (("price_min", price_min), ("price_max", price_max)):
        if value is not None and value < 0:
            return f"{name} must be 0 or more."
    if price_min is not None and price_max is not None and price_min > price_max:
        return "price_min must not be higher than price_max."
    return None


def build_airbnb_payload(
    *,
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    infants: int | None = None,
    pets: int | None = None,
    currency: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    min_rating: float | None = None,
    sort: str | None = None,
    min_bedrooms: int | None = None,
    min_beds: int | None = None,
    min_bathrooms: int | None = None,
    room_types: list[str] | None = None,
) -> dict[str, Any]:
    """Body for the Airbnb listing's `POST /search`. None values are dropped."""
    return _compact(
        {
            "provider": "airbnb",
            "destination": destination,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "infants": infants,
            "pets": pets,
            "currency": currency,
            "price_min": price_min,
            "price_max": price_max,
            "min_rating": min_rating,
            "sort": sort,
            "min_bedrooms": min_bedrooms,
            "min_beds": min_beds,
            "min_bathrooms": min_bathrooms,
            "room_types": room_types or None,
        }
    )
