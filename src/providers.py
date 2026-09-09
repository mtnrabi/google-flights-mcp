"""
Which OTA priced a stay: `providers` on `search_hotels`, and the cross-source
row `compare_hotel_rates` is built out of.

"Provider" here means an accommodation SOURCE -- Booking.com or Airbnb -- and
never an identity provider; `keystore.PROVIDER_GOOGLE` is that other word and
lives in that other module. The vocabulary and the field spelling are taken
from `api_proxy/src/providers.py` deliberately, so the front door and this
server cannot disagree about what a provider is called.

TWO SOURCES, TWO PATHS, AND WHY
-------------------------------
`booking` keeps the path it has always had: straight to
`booking-live-api.p.rapidapi.com` on the caller's own key, billed by RapidAPI
to their own subscription. Nothing about that call moves in this file, which is
the entire point of a default -- a caller who says nothing about providers gets
the request they were getting before this existed, byte for byte.

`airbnb` goes through our own front door, `api.flightpowers.com`, with
`provider=airbnb` in the body (flight_rabbi #472). That is not a preference, it
is where the Airbnb backend is reachable from: `otaLiteAgent` is not on the
RapidAPI edge, and the front is the one place that knows how to reach it,
translate `budget_per_night` into Airbnb's `price_max`, and pass the honest
`degraded` envelope through instead of answering `200 []` for a page it could
not read.

WHOSE KEY, AND THE ONE THING THAT MUST NEVER HAPPEN
---------------------------------------------------
The caller's key, always, and never ours. Each listing is a separate RapidAPI
subscription, so a key subscribed to Booking is not thereby subscribed to
Airbnb: when the Airbnb listing exists and the caller has not bought it, the
Hub answers 403 and this module reports that source as SKIPPED, by name, with
the URL where they subscribe. It does not fall back to a server-side key, it
does not quietly drop the source, and it does not return Booking rows under an
Airbnb request. A silently missing source is indistinguishable from a source
that had nothing, and those are opposite answers.

DEGRADED IS A NAMED ROW, NOT A HOLE
-----------------------------------
A source that was called and could not answer -- the front's
`503 provider_unavailable` while Airbnb is switched off, a `502
search_incomplete`, a timeout -- comes back as its own row with
`search_status: "degraded"`, `count: null` and no prices, and the OTHER
source's rows are still returned. "Booking did not answer" and "Booking had
nothing" have to be sayable as different sentences.

NO CROSS-CURRENCY ARITHMETIC
----------------------------
Every source is asked for the same currency. If two of them answer in
different ones, each row keeps its own and a caveat says the totals are not
comparable. We do not convert: a rate we invented from an exchange rate we did
not fetch would look exactly like a rate somebody quoted.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import httpx

from .rapidapi_client import _compact, _upstream_message, read_quota

#: The request field, spelled the way the front spells it.
PROVIDER_FIELD = "provider"

BOOKING = "booking"
AIRBNB = "airbnb"

#: Every source these tools know about, in the order they are reported.
KNOWN_PROVIDERS: tuple[str, ...] = (BOOKING, AIRBNB)

#: What `search_hotels` does when the caller says nothing. Changing this is a
#: change to every existing integration; it is not a knob.
DEFAULT_SEARCH_PROVIDERS: tuple[str, ...] = (BOOKING,)

#: `compare_hotel_rates` exists to compare, so it asks both and reports the
#: ones the caller has no key for rather than pretending they were not wanted.
DEFAULT_COMPARE_PROVIDERS: tuple[str, ...] = (BOOKING, AIRBNB)

#: Where a caller subscribes, per source. The Airbnb entry is the slug the
#: listing WILL have (`state/gtm/PASTE-airbnb-listing-2026-09-09.md` §1.1); the
#: listing is not published yet, which is why `provider=airbnb` currently comes
#: back degraded from the front rather than 403 from the Hub. It is written
#: down here so the day it goes live nothing else has to be found and changed.
SUBSCRIBE_URLS: dict[str, str] = {
    BOOKING: "https://rapidapi.com/mtnrabi/api/booking-live-api",
    AIRBNB: "https://rapidapi.com/mtnrabi/api/airbnb-live-api",
}

#: The listing name a caller is told to subscribe to, per source.
LISTING_NAMES: dict[str, str] = {
    BOOKING: "Booking Live API",
    AIRBNB: "Airbnb Live API",
}

#: What a review score on that source is out of. Booking rates out of 10 and
#: Airbnb out of 5, so an 8.4 and a 4.9 are not comparable numbers and a row
#: that does not say which scale it is on invites exactly that comparison.
RATING_SCALE: dict[str, int] = {BOOKING: 10, AIRBNB: 5}

#: Whether the totals from that source include tax. `None` on Airbnb is not a
#: "no": we have not established the tax treatment of its display total, and
#: saying so is the honest field value.
TAXES_INCLUDED: dict[str, bool | None] = {BOOKING: True, AIRBNB: None}

#: The route on the api front that serves a hotels search. `provider` rides in
#: the body; the path is deliberately the same one Booking uses, so the front's
#: `tool=hotels-search` attribution histogram does not re-partition itself the
#: day a second source ships (flight_rabbi #472).
API_FRONT_SEARCH_PATH = "/v1/hotels/search"

#: Rule 11. The front OVERWRITES `_fp_source`/`_fp_tool` with its own
#: conclusion, so a caller behind it identifies itself with this header and the
#: front turns it into the body fields it logs. A body field set here would be
#: thrown away.
CLIENT_HEADER = "X-FP-Client"
CLIENT_NAME = "mcp-hotels"

#: Statuses a provider row can carry. `ok`/`empty`/`degraded` mirror the
#: backend's own `search_status` vocabulary (`output_schema.SEARCH_STATUS_VALUES`
#: minus `partial`, which a single-request source cannot be).
PROVIDER_STATUSES = ("ok", "empty", "degraded", "skipped")

#: Why a source was not called at all. `no_key` and `not_subscribed` are
#: different problems with different fixes, and telling a paying subscriber to
#: "get a key" when their key was simply refused is the worst reply available.
SKIP_REASONS = ("no_key", "not_subscribed", "key_rejected", "unsupported")


def client_id(version: str) -> str:
    """The `X-FP-Client` value: who is calling the front, and which build."""
    return f"{CLIENT_NAME}/{version}"


class UnknownProvider(ValueError):
    """A provider name this server does not serve."""


def normalise_providers(
    raw: Sequence[str] | None,
    *,
    default: Sequence[str],
    allowed: Sequence[str] = KNOWN_PROVIDERS,
) -> list[str]:
    """The sources to price, cleaned and de-duplicated in request order.

    `None` means "the caller said nothing", which is the default. An explicit
    empty list is NOT the default -- it is a request to search nothing, which
    is never what anybody meant -- and is rejected rather than silently turned
    into a Booking search the caller did not ask for.
    """
    if raw is None:
        return list(default)
    if isinstance(raw, str):  # a model that sent a bare string, not a list
        raw = [raw]

    names: list[str] = []
    for entry in raw:
        name = str(entry or "").strip().lower()
        if not name:
            continue
        if name not in allowed:
            raise UnknownProvider(
                f"Unknown provider {name!r}. Valid providers are: "
                f"{', '.join(allowed)}."
            )
        if name not in names:
            names.append(name)

    if not names:
        raise UnknownProvider(
            "providers was empty, so there was nothing to search. Omit it for "
            f"the default ({', '.join(default)}), or name one of: "
            f"{', '.join(allowed)}."
        )
    return names


@dataclass
class ProviderOutcome:
    """What one source answered, whether or not it answered well."""

    provider: str
    status: str
    reason: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    subscribe_url: str = ""
    quota: dict[str, int] = field(default_factory=dict)
    #: Only set when the source was reached and echoed one back.
    applied_filters: Any = None

    @property
    def called(self) -> bool:
        """False only for a source that was never contacted."""
        return self.status != "skipped"


def skipped(provider: str, reason: str, detail: str) -> ProviderOutcome:
    """A source that was not called, named with the fix."""
    return ProviderOutcome(
        provider=provider,
        status="skipped",
        reason=reason,
        detail=detail,
        subscribe_url=SUBSCRIBE_URLS.get(provider, ""),
    )


def degraded(provider: str, reason: str, detail: str) -> ProviderOutcome:
    """A source that was called and could not answer."""
    return ProviderOutcome(
        provider=provider, status="degraded", reason=reason, detail=detail
    )


def no_key_detail(provider: str) -> str:
    return (
        f"No RapidAPI key for the {LISTING_NAMES.get(provider, provider)} was "
        "supplied, so it was not called."
    )


def not_subscribed_detail(provider: str, upstream: str) -> str:
    listing = LISTING_NAMES.get(provider, provider)
    upstream = (upstream or "").strip()
    lead = f"{upstream} " if upstream else ""
    return (
        f"{lead}The key supplied is not subscribed to the {listing}, so that "
        "source was not searched. Each source is a separate subscription."
    )


# ── rows ────────────────────────────────────────────────────────────────


def stamp_rows(rows: Iterable[Any], provider: str) -> list[dict[str, Any]]:
    """Copy each row with `provider` and `rating_scale` on it.

    Copied rather than mutated: the row objects come out of a JSON body that
    is also summarised, and a helper that edits its input in place is how a
    summary and the rows it describes start disagreeing.

    `rating_scale` is filled only when the source did not send one. It is a
    property of the source, not a measurement -- Booking's review scores are
    out of 10, Airbnb's out of 5 -- and without it on the row a model
    comparing an 8.4 with a 4.9 has nothing to read.
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        copy = dict(row)
        copy[PROVIDER_FIELD] = provider
        if copy.get("rating_scale") is None and provider in RATING_SCALE:
            copy["rating_scale"] = RATING_SCALE[provider]
        out.append(copy)
    return out


#: The numeric total for a stay, in the order the sources spell it. Strings are
#: never parsed: `price_string` is "US$2,434" on one source and "$423 for 3
#: nights" on another, and a number guessed out of a label is a number nobody
#: quoted.
_TOTAL_FIELDS = ("price_total", "price", "price_as_number")


def row_total(row: Mapping[str, Any]) -> float | None:
    """The stay total on a row, or None when the row carries no price."""
    for name in _TOTAL_FIELDS:
        value = row.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def row_currency(row: Mapping[str, Any]) -> str | None:
    value = row.get("currency")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def summarise(
    outcome: ProviderOutcome, *, requested_currency: str | None, top: int = 3
) -> dict[str, Any]:
    """One provider row for `compare_hotel_rates`.

    `cheapest_total` and `median_total` are computed over the rows that
    actually carry a price, and `count` is that number -- a source that
    returned twenty rows and priced twelve of them reports twelve, because the
    other eight are not evidence about what a stay costs there.
    """
    row: dict[str, Any] = {
        "provider": outcome.provider,
        "search_status": outcome.status,
        "search_reason": outcome.reason,
        "count": None,
        "cheapest_total": None,
        "cheapest_name": None,
        "cheapest_link": None,
        "median_total": None,
        "currency": None,
        "rating_scale": RATING_SCALE.get(outcome.provider),
        "taxes_included": TAXES_INCLUDED.get(outcome.provider),
        "retrieved_at": None,
        "top": [],
    }
    if outcome.detail:
        row["detail"] = outcome.detail

    if outcome.status == "degraded":
        # count stays None on purpose: rule 3. Zero would read as "nothing
        # there", which is the one thing a failed search does not know.
        return row

    priced = [(row_total(r), r) for r in outcome.rows]
    priced = [(total, r) for total, r in priced if total is not None]
    row["count"] = len(priced)

    currencies = {c for c in (row_currency(r) for r in outcome.rows) if c}
    if len(currencies) == 1:
        row["currency"] = next(iter(currencies))
    elif not currencies and requested_currency:
        # Nothing on the rows said, so the only honest answer is the currency
        # the source was ASKED for. Reported as such, never inferred from a
        # price string.
        row["currency"] = requested_currency.strip().upper()

    stamps = sorted(
        {
            r.get("retrieved_at")
            for r in outcome.rows
            if isinstance(r.get("retrieved_at"), str) and r.get("retrieved_at")
        }
    )
    if stamps:
        row["retrieved_at"] = stamps[-1]

    if not priced:
        return row

    priced.sort(key=lambda pair: pair[0])
    cheapest_total, cheapest = priced[0]
    row["cheapest_total"] = cheapest_total
    row["cheapest_name"] = cheapest.get("name")
    row["cheapest_link"] = cheapest.get("link")
    row["median_total"] = statistics.median(total for total, _ in priced)
    row["top"] = [dict(r) for _, r in priced[:top]]
    return row


#: The two sentences that keep a cross-source comparison honest. They are
#: facts about the sources, not about any one search, so they ride on every
#: comparison that involved both.
CAVEAT_RATING_SCALE = (
    "rating_scale differs by source: Booking rates out of 10, Airbnb out of 5."
)
CAVEAT_TAXES = (
    "taxes_included is true on Booking and null on Airbnb; null means we have "
    "not established it, not that taxes are excluded."
)
CAVEAT_CURRENCY = (
    "The sources answered in different currencies ({currencies}). The totals "
    "are not comparable and nothing here was converted."
)
CAVEAT_DEGRADED = (
    "{provider} was asked and did not answer, so it has no count and no "
    "prices. That is not the same as it having nothing."
)
CAVEAT_SKIPPED = (
    "{provider} was not searched ({reason}) and is not counted anywhere in "
    "this comparison. Subscribe at {url}."
)


def caveats_for(
    rows: Sequence[Mapping[str, Any]],
    skipped_rows: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """The caveats these provider rows earn, in a stable order.

    Two sources answering is what earns the scale and tax sentences: on a
    single-source answer there is nothing to compare and the sentences would
    be noise a model has to carry anyway.
    """
    out: list[str] = []
    answered = [r for r in rows if r.get("search_status") != "skipped"]
    if len({r["provider"] for r in answered}) > 1:
        out.append(CAVEAT_RATING_SCALE)
        out.append(CAVEAT_TAXES)

    currencies = sorted({r.get("currency") for r in answered if r.get("currency")})
    if len(currencies) > 1:
        out.append(CAVEAT_CURRENCY.format(currencies=", ".join(currencies)))

    for row in rows:
        if row.get("search_status") == "degraded":
            out.append(CAVEAT_DEGRADED.format(provider=row["provider"]))

    for row in skipped_rows:
        out.append(
            CAVEAT_SKIPPED.format(
                provider=row.get("provider", "a source"),
                reason=row.get("reason", "no key"),
                url=row.get("subscribe_url") or "rapidapi.com/mtnrabi",
            )
        )
    return out


def skipped_payload(outcome: "ProviderOutcome") -> dict[str, Any]:
    """A `providers_skipped` entry: who, why, and where to fix it."""
    return {
        "provider": outcome.provider,
        "reason": outcome.reason,
        "detail": outcome.detail,
        "subscribe_url": outcome.subscribe_url
        or SUBSCRIBE_URLS.get(outcome.provider, ""),
    }


# ── the api front ───────────────────────────────────────────────────────


def build_front_payload(
    *,
    provider: str,
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    currency: str | None = None,
    budget_per_night: int | None = None,
) -> dict[str, Any]:
    """Body for `POST /v1/hotels/search` on the front. Nones are dropped.

    `budget_per_night` is forwarded rather than translated: the front maps it
    onto Airbnb's `price_max` and reports the rewrite in `X-Field-Aliased`, and
    two halves that both do the mapping is how a ceiling gets applied twice.
    """
    return _compact(
        {
            PROVIDER_FIELD: provider,
            "destination": destination,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": currency,
            "budget_per_night": budget_per_night,
        }
    )


def front_headers(*, api_key: str, version: str) -> dict[str, str]:
    return {
        # The front reads `x-api-key`, `x-rapidapi-key` and a bearer; the
        # RapidAPI spelling is used so the same key works unchanged whether a
        # caller reaches Booking directly or Airbnb through the front.
        "x-rapidapi-key": api_key,
        CLIENT_HEADER: client_id(version),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _error_type(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            value = error.get("type")
            if isinstance(value, str) and value:
                return value
        for name in ("type", "error_type"):
            value = body.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def classify_front_response(
    provider: str, response: httpx.Response
) -> ProviderOutcome:
    """Turn one front reply into an outcome. Never raises.

    The mapping, and why each one is what it is:

    * `403` -- the Hub's answer for a key that is not subscribed to THIS
      listing. Skipped, named, with the subscribe URL. Never our key.
    * `401` -- the key was refused outright. Also skipped rather than
      degraded: nothing is wrong upstream, and retrying cannot help.
    * `429` -- the caller's own plan is spent. Degraded, because the source WAS
      asked; a retry would only bill them again for a request that cannot
      succeed.
    * `503 provider_unavailable` -- the front has this source switched off
      (flight_rabbi #472 ships it off). Degraded with the reason, so the model
      can say "Airbnb is not available here" rather than "Airbnb had nothing".
    * `502 search_incomplete` -- the backend read a page it could not parse and
      refused to answer `200 []` for it. Degraded, carrying the backend's own
      `search_reason`.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None

    status = response.status_code

    if status == 403:
        return skipped(
            provider,
            "not_subscribed",
            not_subscribed_detail(provider, _upstream_message(response)),
        )
    if status == 401:
        return skipped(
            provider,
            "key_rejected",
            f"{_upstream_message(response)} The key was refused for the "
            f"{LISTING_NAMES.get(provider, provider)}.",
        )
    if status == 429:
        return degraded(provider, "quota_exhausted", _upstream_message(response))
    if status >= 400:
        reason = _error_type(body) or "upstream_error"
        if isinstance(body, dict) and isinstance(body.get("search_reason"), str):
            reason = body["search_reason"] or reason
        return degraded(provider, reason, _upstream_message(response))

    rows = []
    search_status = ""
    search_reason = ""
    if isinstance(body, dict):
        raw_rows = body.get("properties")
        if raw_rows is None and body.get("name"):
            raw_rows = [body]
        rows = stamp_rows(raw_rows or [], provider)
        for name, target in (("search_status", "s"), ("search_reason", "r")):
            value = body.get(name)
            if isinstance(value, str) and value.strip():
                if target == "s":
                    search_status = value.strip()
                else:
                    search_reason = value.strip()
    elif isinstance(body, list):
        rows = stamp_rows(body, provider)

    if search_status == "degraded":
        # The honest envelope: the backend says it could not read the page, and
        # a 200 with an empty list must not be reported as "nothing there".
        return degraded(provider, search_reason or "degraded", _upstream_message(response))

    if not search_status:
        search_status = "ok" if rows else "empty"
    if not search_reason:
        search_reason = search_status

    return ProviderOutcome(
        provider=provider,
        status=search_status,
        reason=search_reason,
        rows=rows,
        quota=read_quota(response),
    )


async def search_via_front(
    client: httpx.AsyncClient,
    *,
    provider: str,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    version: str,
    timeout_seconds: float,
) -> ProviderOutcome:
    """One search on one source through the api front. Never raises.

    NO RETRY. The front does not retry this lane either, and for the same
    reason: the backend has already concluded the page was unreadable, and a
    second attempt spends a second request to be told so again. A source that
    could not answer comes back degraded, and the tool still returns whatever
    the other source found.
    """
    url = f"{base_url.rstrip('/')}{API_FRONT_SEARCH_PATH}"
    try:
        response = await client.post(
            url,
            json=payload,
            headers=front_headers(api_key=api_key, version=version),
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException:
        return degraded(
            provider,
            "timeout",
            "The source did not respond in time. Nothing was booked or charged.",
        )
    except httpx.HTTPError as exc:  # connection reset, DNS, TLS
        return degraded(provider, "transport_error", str(exc) or exc.__class__.__name__)

    return classify_front_response(provider, response)
