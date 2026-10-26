"""`search_airbnb`, the hotels product's Airbnb tool, behind MCP_AIRBNB.

What would cost something if it broke:

* **Flag off, nothing moved.** Both Claude directory listings are in review,
  so with MCP_AIRBNB unset (or anything but "on") each hostname must serve
  exactly what main served. The goldens in tests/golden/ are that snapshot:
  regenerated from origin/main (11c92fb) with `tests/golden/regenerate.py`
  they come out byte-identical to the committed files, and this PR does not
  touch them.
* **Flag on, hotels only.** The tool appears on the hotels host and nowhere
  else, and `search_hotels` stays byte-identical beside it.
* **The caller's key, the Airbnb listing.** One request to
  airbnb-live-api.p.rapidapi.com/search on the caller's own key, attributed
  (rule 11), never on ours: the keyless allowance is refused.
* **A missing subscription is said out loud**, with the listing URL, never an
  empty list that reads as "no stays".
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import src.server as server_module
from src.entrypoint import build_entrypoint
from src.hotel_compact import AIRBNB_COMPACT_FIELDS
from src.hotels_client import AIRBNB_HOST, AIRBNB_SIGNUP_URL
from src.settings import load_settings
from src.trial import KEYED_SOURCE, utc_day
from tests.golden.regenerate import capture
from tests.test_host_routing import (
    FLIGHTS_ALIAS,
    FLIGHTS_HOST,
    HOTELS_HOST,
    MCP_ENV,
    PUBLIC_URLS,
    golden,
)
from tests.test_oauth import HEADER_KEY, SUB, call_tool
from tests.test_server import KEY, build_with_upstream

#: The caller's own key, as a request header. Different from KEY, which the
#: test builds configure as the deployment's RAPIDAPI_KEY fallback.
CALLER_KEY = "caller-" + "c" * 43
from tests.test_trial import TaggedUpstream, _bearer, _build_trial
from tests.test_oauth import Session

STAY = {
    "destination": "Lisbon",
    "checkin_date": "2027-03-05",
    "checkout_date": "2027-03-08",
}


def airbnb_row(i: int, total: float | None) -> dict:
    """A row in the Airbnb Live API's common schema (hotel_agent
    common/schema.js PROPERTY_KEYS), as airbnb.js normalizeRow fills it."""
    return {
        "name": f"Loft {i}",
        "price_string": None if total is None else f"${total:g} for 3 nights",
        "price": total,
        "review_score": 4.8,
        "review_count": 100 + i,
        "room_type": "Loft",
        "location": "Alfama",
        "image_url": f"https://a0.muscache.com/im/pictures/{i}.jpg",
        "link": f"https://www.airbnb.com/rooms/{1000 + i}",
        "nights": 3,
        "adults": 2,
        "children": None,
        "provider": "airbnb",
        "provider_property_id": str(1000 + i),
        "latitude": 38.71,
        "longitude": -9.13,
        "price_total": total,
        "price_per_night": None if total is None else round(total / 3, 2),
        "price_original_total": None,
        "currency": "EUR",
        "taxes_included": None,
        "rating": 4.8,
        "rating_scale": 5,
        "beds": 2,
        "bedrooms": 1,
        "baths": 1,
        "available": total is not None,
        "retrieved_at": "2026-10-23T06:00:00Z",
    }


#: 12 rows; the cheapest (row 11, 150) sits past the default 10.
ROWS = [airbnb_row(i, 400.0 - i * 10) for i in range(11)] + [airbnb_row(11, 150.0)]
ENVELOPE = {
    "provider": "airbnb",
    **STAY,
    "filters_applied": {
        "currency": {"value": "EUR", "applied_by": "airbnb", "post_filter": False}
    },
    "search_status": "ok",
    "search_reason": "ok",
    "properties": ROWS,
}


class Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


async def call(mcp, **kwargs):
    async with Client(mcp) as client:
        out = await client.call_tool("search_airbnb", kwargs)
    return out.structured_content


async def tool_names(mcp) -> list[str]:
    async with Client(mcp) as client:
        return [t.name for t in await client.list_tools()]


# ── (a) flag off: each hostname serves exactly main's surface ────────────


def _combined(monkeypatch, airbnb: str | None):
    """The deployed shape: MCP_PRODUCTS=flights plus the built-in host map."""
    for name in (*MCP_ENV, "MCP_AIRBNB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", "flights")
    for name, value in PUBLIC_URLS.items():
        monkeypatch.setenv(name, value)
    if airbnb is not None:
        monkeypatch.setenv("MCP_AIRBNB", airbnb)
    return build_entrypoint()


OFF_VALUES = [None, "", "off", "0", "true", "yes", "1", "ON please"]


class TestFlagOffIsMain:
    @pytest.mark.parametrize("value", OFF_VALUES)
    def test_only_exactly_on_turns_it_on(self, monkeypatch, value):
        monkeypatch.delenv("MCP_AIRBNB", raising=False)
        if value is not None:
            monkeypatch.setenv("MCP_AIRBNB", value)
        assert load_settings("hotels").airbnb_enabled is False

    @pytest.mark.parametrize("value", ["on", "ON", " on ", '"on"'])
    def test_on_is_on(self, monkeypatch, value):
        monkeypatch.setenv("MCP_AIRBNB", value)
        assert load_settings("hotels").airbnb_enabled is True

    @pytest.mark.parametrize("value", [None, "off", "true"])
    @pytest.mark.parametrize(
        "host, product",
        [(FLIGHTS_HOST, "flights"), (FLIGHTS_ALIAS, "flights"), (HOTELS_HOST, "hotels")],
    )
    async def test_every_host_serves_the_main_snapshot(
        self, monkeypatch, value, host, product
    ):
        served = await capture(_combined(monkeypatch, value).app, host)
        expected = golden(product)
        assert [t["name"] for t in served["tools"]] == [
            t["name"] for t in expected["tools"]
        ]
        # names, descriptions, inputSchema, outputSchema, annotations, title:
        # the whole serialised tool, byte for byte.
        assert json.dumps(served["tools"], sort_keys=True) == json.dumps(
            expected["tools"], sort_keys=True
        )
        assert served == expected

    async def test_a_hand_built_settings_is_off(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]), products="hotels")
        assert "search_airbnb" not in await tool_names(mcp)


# ── (b) flag on: hotels host only, search_hotels untouched ───────────────


class TestFlagOn:
    async def test_hotels_host_adds_only_search_airbnb(self, monkeypatch):
        served = await capture(_combined(monkeypatch, "on").app, HOTELS_HOST)
        expected = golden("hotels")
        names = [t["name"] for t in served["tools"]]
        assert names == [t["name"] for t in expected["tools"]] + ["search_airbnb"]
        # Every tool main served, search_hotels included, is byte-identical.
        for got, want in zip(served["tools"], expected["tools"]):
            assert json.dumps(got, sort_keys=True) == json.dumps(
                want, sort_keys=True
            ), want["name"]
        assert served["instructions"] == expected["instructions"]
        assert served["serverInfo"] == expected["serverInfo"]

    @pytest.mark.parametrize("host", [FLIGHTS_HOST, FLIGHTS_ALIAS])
    async def test_flights_host_is_unchanged(self, monkeypatch, host):
        served = await capture(_combined(monkeypatch, "on").app, host)
        assert served == golden("flights")

    @pytest.mark.parametrize("products", ["flights", "both"])
    async def test_absent_everywhere_but_the_hotels_product(self, products):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products=products,
            airbnb_enabled=True,
        )
        assert "search_airbnb" not in await tool_names(mcp)

    async def test_title_annotations_and_schema(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products="hotels",
            airbnb_enabled=True,
        )
        async with Client(mcp) as client:
            tools = {t.name: t.model_dump(mode="json") for t in await client.list_tools()}
        tool = tools["search_airbnb"]
        assert tool["title"] == "FlightPowers: search Airbnb stays"
        assert tool["annotations"] == {
            "title": "FlightPowers: search Airbnb stays",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
        props = tool["inputSchema"]["properties"]
        assert set(props) == {
            "destination", "checkin_date", "checkout_date", "adults", "children",
            "currency", "price_min", "price_max", "min_rating", "sort",
            "infants", "pets", "min_bedrooms", "min_beds", "min_bathrooms",
            "room_types", "limit", "verbose",
        }
        assert tool["inputSchema"]["required"] == [
            "destination", "checkin_date", "checkout_date"
        ]
        assert props["sort"]["anyOf"][0]["enum"] == ["price_asc", "price_desc", "rating_desc"]
        assert props["room_types"]["anyOf"][0]["items"]["enum"] == [
            "Entire home/apt", "Private room", "Shared room", "Hotel room"
        ]
        assert props["limit"]["anyOf"][0]["minimum"] == 1
        text = tool["description"]
        assert "whether they include taxes is not stated by the page" in text
        assert "FlightPowers is not affiliated with Airbnb" in text
        # Plan and subscription wording lives in the refusal text only (the
        # #537 rule: tool text states facts; a directory reviewer may read
        # plan wording as promotion).
        for promo in ("BASIC", "plan", "subscri", "RapidAPI", "Booking Live API"):
            assert promo not in text, promo
        # price_min / price_max wording comes from one constant.
        from src.server import AIRBNB_PRICE_BASIS

        for name in ("price_min", "price_max"):
            assert AIRBNB_PRICE_BASIS in props[name]["description"], name
        assert tool["outputSchema"]["required"] == ["results"]
        # Its own api_usage: always the caller's key, one request, no retry.
        usage = json.dumps(tool["outputSchema"]["properties"]["api_usage"])
        for word in ("trial", "free_searches", "retried", "retry"):
            assert word not in usage, word

    async def test_a_bad_room_type_or_sort_costs_nothing(self, caller_key):
        recorder = Recorder(httpx.Response(200, json=ENVELOPE))
        mcp = build_with_upstream(recorder, products="hotels", airbnb_enabled=True)
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await call(mcp, **STAY, room_types=["Castle"])
        with pytest.raises(ToolError):
            await call(mcp, **STAY, sort="cheapest")
        assert recorder.requests == []


# ── (c) one keyed call: host, key, attribution, compact rows ─────────────


@pytest.fixture
def caller_key(monkeypatch):
    """The in-memory client carries no HTTP request, so the request context
    is stubbed with the header a real caller sends."""
    monkeypatch.setattr(
        server_module,
        "_request_context",
        lambda: ({"x-rapidapi-key": CALLER_KEY}, {}),
    )
    return CALLER_KEY


@pytest.mark.usefixtures("caller_key")
class TestOneCall:
    def _mcp(self, response: httpx.Response):
        recorder = Recorder(response)
        mcp = build_with_upstream(recorder, products="hotels", airbnb_enabled=True)
        return mcp, recorder

    async def test_request_goes_to_the_airbnb_listing_on_the_callers_key(self):
        mcp, recorder = self._mcp(
            httpx.Response(
                200,
                json=ENVELOPE,
                headers={
                    "x-ratelimit-requests-remaining": "99",
                    "x-ratelimit-requests-limit": "100",
                },
            )
        )
        await call(
            mcp, **STAY, adults=2, children=1, infants=1, pets=1, currency="eur",
            price_min=50, price_max=300, min_rating=4.5, sort="price_asc",
            min_bedrooms=1, min_beds=2, min_bathrooms=1,
            room_types=["Entire home/apt"],
        )
        (request,) = recorder.requests
        assert request.method == "POST"
        assert request.url.host == AIRBNB_HOST == "airbnb-live-api.p.rapidapi.com"
        assert request.url.path == "/search"
        # The caller's key, not the deployment's fallback (KEY).
        assert request.headers["x-rapidapi-key"] == CALLER_KEY != KEY
        assert request.headers["x-rapidapi-host"] == AIRBNB_HOST
        body = json.loads(request.content)
        assert body == {
            "provider": "airbnb",
            **STAY,
            "adults": 2,
            "children": 1,
            "infants": 1,
            "pets": 1,
            "currency": "eur",
            "price_min": 50,
            "price_max": 300,
            "min_rating": 4.5,
            "sort": "price_asc",
            "min_bedrooms": 1,
            "min_beds": 2,
            "min_bathrooms": 1,
            "room_types": ["Entire home/apt"],
            # Rule 11, the same tags the Booking path sends on a keyed call.
            "_fp_source": KEYED_SOURCE,
            "_fp_tool": "search_airbnb",
        }

    async def test_default_rows_are_compact_bounded_and_keep_the_cheapest(self):
        mcp, _ = self._mcp(httpx.Response(200, json=ENVELOPE))
        out = await call(mcp, **STAY)
        assert out["result_count"] == 10
        assert out["results_total"] == 12
        assert "12 stays were found" in out["results_note"]
        assert all(list(r) == list(AIRBNB_COMPACT_FIELDS) for r in out["results"])
        assert out["results"][-1]["name"] == "Loft 11"  # the cheapest, kept
        assert out["results"][0] == {
            "name": "Loft 0",
            "price_total": 400.0,
            "price_per_night": 133.33,
            "currency": "EUR",
            "rating": 4.8,
            "rating_scale": 5,
            "review_count": 100,
            "room_type": "Loft",
            "bedrooms": 1,
            "beds": 2,
            "baths": 1,
            "taxes_included": None,
            "link": "https://www.airbnb.com/rooms/1000",
            "retrieved_at": "2026-10-23T06:00:00Z",
        }
        assert out["search_status"] == "ok"
        assert out["filters_applied"] == ENVELOPE["filters_applied"]
        assert out["api_usage"]["hub_requests_billed"] == 1
        assert "Airbnb Live API subscription" in out["api_usage"]["note"]

    async def test_limit_and_verbose(self):
        mcp, _ = self._mcp(httpx.Response(200, json=ENVELOPE))
        out = await call(mcp, **STAY, limit=3)
        assert [r["name"] for r in out["results"]] == ["Loft 0", "Loft 1", "Loft 11"]
        out = await call(mcp, **STAY, verbose=True)
        assert out["results"] == ROWS
        assert "results_total" not in out
        out = await call(mcp, **STAY, verbose=True, limit=2)
        assert out["results"] == [ROWS[0], ROWS[11]]

    async def test_an_empty_city_is_an_empty_answer(self):
        envelope = {**ENVELOPE, "search_status": "empty", "search_reason": "no_results", "properties": []}
        mcp, _ = self._mcp(httpx.Response(200, json=envelope))
        out = await call(mcp, **STAY)
        assert out["results"] == [] and out["search_status"] == "empty"
        assert "needs_api_key" not in out

    async def test_a_degraded_page_is_an_error_not_an_empty_list(self):
        from fastmcp.exceptions import ToolError

        degraded = {
            "provider": "airbnb", **STAY, "search_status": "degraded",
            "search_reason": "blocked_page",
            "detail": "Airbnb answered 200 and the response is not a results page",
            "properties": [],
        }
        mcp, recorder = self._mcp(httpx.Response(502, json=degraded))
        with pytest.raises(ToolError, match="not a results page"):
            await call(mcp, **STAY)
        # Not retried: the listing bills every request.
        assert len(recorder.requests) == 1

    @pytest.mark.parametrize("status", [500, 503, 504])
    async def test_a_server_error_is_one_request(self, status):
        from fastmcp.exceptions import ToolError

        mcp, recorder = self._mcp(httpx.Response(status, json={"detail": "boom"}))
        with pytest.raises(ToolError):
            await call(mcp, **STAY)
        assert len(recorder.requests) == 1

    async def test_a_spent_plan_is_an_error(self):
        from fastmcp.exceptions import ToolError

        mcp, recorder = self._mcp(
            httpx.Response(429, json={"message": "You have exceeded the MONTHLY quota"})
        )
        with pytest.raises(ToolError, match="MONTHLY quota") as caught:
            await call(mcp, **STAY)
        assert "Booking" not in str(caught.value)
        assert len(recorder.requests) == 1

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"detail": "something"},
            {"search_status": "degraded", "properties": []},
            {"search_status": "ok"},
        ],
    )
    async def test_an_unreadable_200_is_an_error_not_an_empty_list(self, body):
        from fastmcp.exceptions import ToolError

        mcp, _ = self._mcp(httpx.Response(200, json=body))
        with pytest.raises(ToolError, match="without a readable result"):
            await call(mcp, **STAY)

    async def test_a_timeout_promises_no_free_retry(self):
        """The shared Booking text says "Retrying usually succeeds; nothing
        was booked or charged": false for a listing called once that bills
        every request it receives."""
        from fastmcp.exceptions import ToolError

        requests: list[httpx.Request] = []

        def slow(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            raise httpx.ReadTimeout("too slow", request=request)

        mcp = build_with_upstream(slow, products="hotels", airbnb_enabled=True)
        with pytest.raises(ToolError) as caught:
            await call(mcp, **STAY)
        text = str(caught.value)
        assert "Airbnb Live API did not respond in time" in text
        assert "may still count the request" in text
        assert "charged" not in text and "Retrying" not in text
        assert len(requests) == 1


# ── (c2) a bad input is refused before it is billed ──────────────────────


class TestBadInputCostsNothing:
    """Every request that reaches the listing is billed, its 400s too, so
    what the Lambda would refuse is refused here."""

    @pytest.mark.parametrize(
        "change, needle",
        [
            ({"checkin_date": "2027/03/05"}, "checkin_date must be a calendar date"),
            ({"checkout_date": "2027-02-31"}, "checkout_date must be a calendar date"),
            ({"checkin_date": "20270305"}, "checkin_date must be a calendar date"),
            ({"checkin_date": "2020-01-01"}, "is in the past"),
            ({"checkout_date": "2027-03-05"}, "checkout_date must be after"),
            ({"checkout_date": "2027-03-01"}, "checkout_date must be after"),
            ({"adults": 0}, "adults must be 1 or more"),
            ({"children": -1}, "children must be 0 or more"),
            ({"min_beds": -2}, "min_beds must be 0 or more"),
            ({"min_rating": 9}, "min_rating is out of 5"),
            ({"price_max": -5}, "price_max must be 0 or more"),
            ({"price_min": 200, "price_max": 100}, "price_min must not be higher"),
        ],
    )
    async def test_refused_without_a_request(self, caller_key, change, needle):
        from fastmcp.exceptions import ToolError

        recorder = Recorder(httpx.Response(200, json=ENVELOPE))
        mcp = build_with_upstream(recorder, products="hotels", airbnb_enabled=True)
        with pytest.raises(ToolError, match=needle) as caught:
            await call(mcp, **{**STAY, **change})
        assert "Nothing was searched or billed" in str(caught.value)
        assert recorder.requests == []

    async def test_edges_that_are_real_searches_go_through(self, caller_key):
        recorder = Recorder(httpx.Response(200, json=ENVELOPE))
        mcp = build_with_upstream(recorder, products="hotels", airbnb_enabled=True)
        out = await call(
            mcp, **STAY, adults=1, children=0, pets=0, min_rating=5,
            price_min=100, price_max=100, min_bedrooms=0,
        )
        assert out["search_status"] == "ok"
        assert len(recorder.requests) == 1

    def test_today_utc_is_not_the_past(self):
        from datetime import date

        from src.hotels_client import airbnb_input_problem

        today = date(2027, 3, 5)
        args = dict(
            counts={}, min_rating=None, price_min=None, price_max=None, today=today
        )
        assert airbnb_input_problem(
            checkin_date="2027-03-05", checkout_date="2027-03-06", **args
        ) is None
        assert "past" in airbnb_input_problem(
            checkin_date="2027-03-04", checkout_date="2027-03-06", **args
        )



class TestOverTheRealTransport:
    async def test_a_header_key_is_the_one_that_goes_upstream(self, monkeypatch):
        """Over the real transport: the caller's own `x-rapidapi-key`."""
        monkeypatch.setenv("MCP_AIRBNB", "on")
        deployment = _build_trial(monkeypatch, products="hotels")
        upstream = AirbnbUpstream()
        monkeypatch.setattr(
            server_module,
            "_shared_client",
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        async with Session(deployment) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                STAY,
                {"authorization": f"Bearer {token}", "x-rapidapi-key": HEADER_KEY},
                tool="search_airbnb",
            )
        assert upstream.airbnb_keys == [HEADER_KEY]
        assert reply["result_count"] == 10
        assert await deployment.trial_store.usage(SUB, utc_day()) == 0


class AirbnbUpstream(TaggedUpstream):
    """The trial harness's fake Google token endpoint, plus the Airbnb host."""

    def __init__(self) -> None:
        super().__init__()
        self.airbnb_keys: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == AIRBNB_HOST:
            self.airbnb_keys.append(request.headers.get("x-rapidapi-key", ""))
            return httpx.Response(200, json=ENVELOPE)
        return super().__call__(request)


# ── (d) not subscribed: the subscribe line, never an empty answer ────────


class TestNotSubscribed:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_key_gets_the_subscribe_line(self, caller_key, status):
        recorder = Recorder(
            httpx.Response(status, json={"message": "You are not subscribed to this API."})
        )
        mcp = build_with_upstream(recorder, products="hotels", airbnb_enabled=True)
        out = await call(mcp, **STAY)
        assert out["needs_api_key"] is True
        assert out["results"] == [] and out["result_count"] == 0
        assert out["signup_url"] == AIRBNB_SIGNUP_URL
        assert "You are not subscribed to this API." in out["message"]
        assert "Google Flights Live API subscription does not cover it" in out["message"]
        assert "https://rapidapi.com/mtnrabi/api/airbnb-live-api" in out["message"]
        assert "BASIC plan is free with 100 searches a month" in out["message"]
        assert "PRO is $10 a month for 10,000 searches" in out["message"]
        assert len(recorder.requests) == 1  # not retried: a 403 is not a fault

    @pytest.mark.parametrize("fallback", ["", KEY])
    async def test_no_caller_key_gets_the_subscribe_line(self, fallback):
        """With no key from the caller, the deployment's own RAPIDAPI_KEY
        fallback (KEY) is not spent on this listing either."""
        recorder = Recorder(httpx.Response(200, json=ENVELOPE))
        mcp = build_with_upstream(
            recorder, products="hotels", airbnb_enabled=True,
            fallback_rapidapi_key=fallback,
        )
        out = await call(mcp, **STAY)
        assert out["needs_api_key"] is True
        assert out["signup_url"] == AIRBNB_SIGNUP_URL
        assert "airbnb-live-api" in out["message"]
        assert "BASIC plan is free with 100 searches a month" in out["message"]
        assert "PRO is $10 a month for 10,000 searches" in out["message"]
        assert recorder.requests == []


# ── (e) the keyless allowance is refused, never routed to the listing ────


class TestTrialRefused:
    async def test_a_signed_in_caller_with_no_key_is_refused(self, monkeypatch):
        monkeypatch.setenv("MCP_AIRBNB", "on")
        deployment = _build_trial(monkeypatch, products="hotels")
        upstream = AirbnbUpstream()
        monkeypatch.setattr(
            server_module,
            "_shared_client",
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        async with Session(deployment) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                STAY,
                {"authorization": f"Bearer {token}"},
                tool="search_airbnb",
            )
        assert reply["needs_api_key"] is True
        assert reply["results"] == []
        assert reply["signup_url"] == AIRBNB_SIGNUP_URL
        assert "BASIC plan is free with 100 searches a month" in reply["message"]
        assert "PRO is $10 a month for 10,000 searches" in reply["message"]
        # Never sent, never counted: our trial key is not subscribed to it.
        assert upstream.airbnb_keys == []
        assert await deployment.trial_store.usage(SUB, utc_day()) == 0


# ── (f) an identified caller with no stored key gets the reconnect reply ──


class TestStoreMissGetsTheReconnectReply:
    """Sending a caller who already has a key through "get a key" is the
    worst answer available (server.py STORE_MISS_SOURCES)."""

    def _deployment(self, monkeypatch):
        monkeypatch.setenv("MCP_AIRBNB", "on")
        deployment = _build_trial(
            monkeypatch, products="hotels", cap=0, trial_key=""
        )
        upstream = AirbnbUpstream()
        monkeypatch.setattr(
            server_module,
            "_shared_client",
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        return deployment, upstream

    async def test_signed_in_with_no_key(self, monkeypatch):
        deployment, upstream = self._deployment(monkeypatch)
        async with Session(deployment) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                STAY,
                {"authorization": f"Bearer {token}"},
                tool="search_airbnb",
            )
        assert reply["needs_api_key"] is True
        assert reply["results"] == []
        assert "signed in as" in reply["message"]
        assert "/connect" in reply["message"]
        assert "airbnb-live-api" in reply["message"]
        assert reply["signup_url"] == AIRBNB_SIGNUP_URL
        assert reply["how_to_get_a_key"]["connect_url"].endswith("/connect")
        # Not the Booking / flights PRO line, which _reconnect_message's
        # `how` steps quote: this listing's own plan line instead.
        assert "how" not in reply["how_to_get_a_key"]
        assert "2,500" not in json.dumps(reply) and "2,000" not in json.dumps(reply)
        assert "PRO is $10 a month for 10,000 searches" in reply["message"]
        assert reply["message"].count("Nothing was searched") == 1
        assert upstream.airbnb_keys == []

    async def test_a_disconnected_connect_token(self, monkeypatch):
        import re as _re

        from tests.test_oauth import EMAIL, sign_in

        deployment, upstream = self._deployment(monkeypatch)
        async with Session(deployment) as session:
            http = session.http
            await deployment.key_store.put(SUB, EMAIL, "user-key-" + "b" * 36)
            await sign_in(http)
            page_ = (await http.get("/connect")).text
            token = _re.search(r"fp_token=(fpk_[^\s&<]+)", page_).group(1)
            await deployment.key_store.revoke(SUB)
            reply = await call_tool(
                http,
                "/mcp",
                STAY,
                {"authorization": f"Bearer {token}"},
                tool="search_airbnb",
            )
        assert reply["needs_api_key"] is True
        assert "disconnected" in reply["message"]
        assert "signed in as" not in reply["message"]
        assert "airbnb-live-api" in reply["message"]
        assert upstream.airbnb_keys == []
