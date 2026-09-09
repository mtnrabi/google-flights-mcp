"""
Two sources on one hotels server: `providers`, and `compare_hotel_rates`.

The four things that can go wrong here are all invisible from a green
response, so each one has a test named after it:

1. **The default moves.** `search_hotels` has paying callers. A caller who
   says nothing about providers must get the request they were getting before
   this existed and the response they were getting before this existed --
   byte for byte, upstream request included.
2. **A source is billed to us.** Each listing is its own subscription. A
   source the caller has no key for is named and skipped; it is never called
   on a server-side key, and the caller's key is the only key that leaves this
   process.
3. **A failure looks like an absence.** A source that was asked and could not
   answer must come back as its own row with `count: null`, while the other
   source's rows still arrive. "Booking did not answer" and "Booking had
   nothing" are opposite answers.
4. **Two numbers get compared that are not comparable.** Ratings out of 10 and
   out of 5, totals in two currencies, a count that includes rows with no
   price.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import providers as ota
from src.credentials import Credential, resolve_provider_credential
from src.providers import AIRBNB, BOOKING
from src.server import SERVER_VERSION
from tests.test_server import KEY, build_with_upstream

AIRBNB_KEY = "airbnbaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

BOOKING_ROW = {
    "name": "Kremlin Palace",
    "price_string": "US$2,434",
    "price": 2434,
    "review_score": 8.4,
    "review_count": 701,
    "room_type": "Superior Double or Twin Room",
    "link": "https://www.booking.com/hotel/tr/kremlin-palace.html",
    "currency": "USD",
    "retrieved_at": "2026-09-09T18:00:00.000Z",
}

# Shapes taken from the merged Airbnb parser's own output over the 2026-09-09
# Rome capture (state/gtm/PASTE-airbnb-listing-2026-09-09.md §1.4). Prices are
# that capture's, not invented ones, so a reader can check the arithmetic.
AIRBNB_ROWS = [
    {
        "name": "Villa San Giuseppe",
        "price_string": "$423 for 3 nights",
        "price": 423,
        "price_total": 423,
        "price_per_night": 140.72,
        "rating": 4.86,
        "review_count": 481,
        "link": "https://www.airbnb.com/rooms/1083378667420950029",
        "currency": "USD",
        "retrieved_at": "2026-09-09T18:49:23.735Z",
    },
    {
        "name": "Loft between Trevi Fountain and Barberini Metro",
        "price_string": "$589 for 3 nights",
        "price": 589,
        "price_total": 589,
        "price_per_night": 196.12,
        "rating": 4.59,
        "review_count": 132,
        "link": "https://www.airbnb.com/rooms/579636949960261327",
        "currency": "USD",
        "retrieved_at": "2026-09-09T18:49:23.735Z",
    },
]

STAY = {
    "destination": "Rome, Italy",
    "checkin_date": "2026-10-15",
    "checkout_date": "2026-10-18",
}


class Recorder:
    """A MockTransport handler that answers per host and keeps every request.

    Per HOST rather than per URL on purpose: the one thing this whole feature
    must not do is send an Airbnb request to the Booking listing or the other
    way round, and a handler keyed on the hostname is what makes that visible.
    """

    def __init__(self, booking=None, airbnb=None):
        self.requests: list[httpx.Request] = []
        self._booking = booking or (
            lambda _r: httpx.Response(200, json={"properties": [BOOKING_ROW]})
        )
        self._airbnb = airbnb or (
            lambda _r: httpx.Response(200, json={"properties": list(AIRBNB_ROWS)})
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "flightpowers.com" in request.url.host:
            return self._airbnb(request)
        return self._booking(request)

    def to(self, host_fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if host_fragment in r.url.host]

    def body(self, host_fragment: str) -> dict:
        sent = self.to(host_fragment)
        assert sent, f"nothing was sent to {host_fragment}"
        return json.loads(sent[0].content)

    @property
    def keys_sent(self) -> set[str]:
        return {
            r.headers.get("x-rapidapi-key", "")
            for r in self.requests
            if r.headers.get("x-rapidapi-key")
        }


async def call(mcp, tool: str, **kwargs):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, kwargs)
        return result.structured_content


# ── 1. the argument itself ──────────────────────────────────────────────


class TestNormalisingTheArgument:
    def test_none_is_the_default(self):
        assert ota.normalise_providers(None, default=(BOOKING,)) == [BOOKING]

    def test_order_and_duplicates(self):
        got = ota.normalise_providers(
            ["Airbnb", "booking", "AIRBNB"], default=(BOOKING,)
        )
        assert got == [AIRBNB, BOOKING]

    def test_a_bare_string_is_tolerated(self):
        """Models send `"airbnb"` where the schema says `["airbnb"]` often
        enough that rejecting it costs a call for nothing."""
        assert ota.normalise_providers("airbnb", default=(BOOKING,)) == [AIRBNB]

    def test_an_unknown_name_is_rejected_with_the_valid_ones(self):
        with pytest.raises(ota.UnknownProvider) as exc:
            ota.normalise_providers(["expedia"], default=(BOOKING,))
        assert "booking" in str(exc.value) and "airbnb" in str(exc.value)

    def test_an_empty_list_is_rejected_rather_than_becoming_the_default(self):
        """`providers=[]` is a request to search nothing. Turning it into a
        Booking search would answer a question nobody asked."""
        with pytest.raises(ota.UnknownProvider):
            ota.normalise_providers([], default=(BOOKING,))


# ── 2. one key per source, in rule 6's order ────────────────────────────


class TestKeyResolutionPerProvider:
    SHARED = Credential(key=KEY, source="header:x-rapidapi-key")

    def test_the_shared_key_is_used_when_no_source_specific_one_exists(self):
        """The common case: one RapidAPI key subscribed to several listings."""
        got = resolve_provider_credential(AIRBNB, {}, {}, self.SHARED)
        assert got.key == KEY

    def test_a_source_specific_header_wins(self):
        got = resolve_provider_credential(
            AIRBNB, {"x-rapidapi-key-airbnb": AIRBNB_KEY}, {}, self.SHARED
        )
        assert got.key == AIRBNB_KEY
        assert got.source == "header:x-rapidapi-key-airbnb"

    def test_a_source_specific_query_name_wins(self):
        got = resolve_provider_credential(
            AIRBNB, {}, {"rapidapi_key_airbnb": AIRBNB_KEY}, self.SHARED
        )
        assert got.key == AIRBNB_KEY

    def test_a_source_specific_config_field_wins(self):
        """Smithery's blob, with a per-source field inside it."""
        import base64

        blob = base64.b64encode(
            json.dumps({"rapidApiKeyAirbnb": AIRBNB_KEY}).encode()
        ).decode()
        got = resolve_provider_credential(AIRBNB, {}, {"config": blob}, self.SHARED)
        assert got.key == AIRBNB_KEY

    def test_a_gateways_own_generic_key_is_still_not_mistaken_for_ours(self):
        """Rule 6, unchanged: `?api_key=<gateway key>&config=<user config>`.
        A per-source lookup must not reopen the generic channel that the
        unscoped resolver deliberately closes when `config` is present."""
        got = resolve_provider_credential(
            AIRBNB,
            {},
            {"api_key": "smithery-own-key-aaaaaaaaaaaaaaaaaaaaaaa", "config": "{}"},
            Credential(key="", source="none"),
        )
        assert not got.present

    def test_no_key_anywhere_is_reported_rather_than_invented(self):
        got = resolve_provider_credential(
            AIRBNB, {}, {}, Credential(key="", source="none")
        )
        assert not got.present


# ── 3. the default did not move ─────────────────────────────────────────


class TestTheDefaultDidNotMove:
    """The promise to every existing subscriber of `search_hotels`."""

    @pytest.mark.asyncio
    async def test_omitting_providers_is_byte_identical_to_the_old_response(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "search_hotels", **STAY)

        assert set(out) == {"results", "result_count", "api_usage"}
        assert out["results"] == [BOOKING_ROW]
        # Not stamped: a single-source answer has one source, and adding a
        # field to it would be a schema change for callers who asked for
        # nothing.
        assert "provider" not in out["results"][0]
        assert "providers" not in out
        assert "caveats" not in out

    @pytest.mark.asyncio
    async def test_the_upstream_request_is_byte_identical_too(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(mcp, "search_hotels", **STAY)

        assert len(recorder.requests) == 1
        sent = recorder.requests[0]
        assert sent.url.host == "booking-live-api.p.rapidapi.com"
        assert json.loads(sent.content) == STAY
        assert "provider" not in json.loads(sent.content)

    @pytest.mark.asyncio
    async def test_naming_booking_explicitly_is_the_same_path(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        default = await call(mcp, "search_hotels", **STAY)
        explicit = await call(mcp, "search_hotels", providers=["booking"], **STAY)
        assert explicit == default
        assert [json.loads(r.content) for r in recorder.requests] == [STAY, STAY]

    @pytest.mark.asyncio
    async def test_find_hotel_by_name_gained_no_providers_argument(self):
        """It stays Booking-only: Airbnb's room page carries no price, so a
        name lookup there would resolve to something that cannot be priced."""
        mcp = build_with_upstream(Recorder(), fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert "providers" not in tools["find_hotel_by_name"].inputSchema["properties"]
        assert "providers" in tools["search_hotels"].inputSchema["properties"]


# ── 4. both sources ─────────────────────────────────────────────────────


class TestBothSources:
    @pytest.mark.asyncio
    async def test_rows_are_merged_and_each_row_says_where_it_came_from(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )

        assert out["result_count"] == 3
        assert [r["provider"] for r in out["results"]] == [
            BOOKING,
            AIRBNB,
            AIRBNB,
        ]
        # The scale rides on the row, because an 8.4 and a 4.86 are not
        # comparable numbers and nothing else on the row says so.
        assert out["results"][0]["rating_scale"] == 10
        assert out["results"][1]["rating_scale"] == 5

    @pytest.mark.asyncio
    async def test_airbnb_goes_to_the_api_front_with_the_provider_field(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(mcp, "search_hotels", providers=["airbnb"], **STAY)

        front = recorder.to("flightpowers.com")
        assert len(front) == 1
        assert front[0].url.path == "/v1/hotels/search"
        assert recorder.body("flightpowers.com")["provider"] == "airbnb"
        # Nothing went to the Booking listing: an Airbnb request must never be
        # answered with Booking rows.
        assert recorder.to("rapidapi.com") == []

    @pytest.mark.asyncio
    async def test_the_front_call_is_attributable_and_carries_the_callers_key(self):
        """Rule 11. The front OVERWRITES body attribution with its own
        conclusion, so `X-FP-Client` is the only thing that identifies us --
        and a 200 proves nothing about attribution."""
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(mcp, "search_hotels", providers=["airbnb"], **STAY)

        sent = recorder.to("flightpowers.com")[0]
        assert sent.headers["x-fp-client"] == f"mcp-hotels/{SERVER_VERSION}"
        assert sent.headers["x-rapidapi-key"] == KEY

    @pytest.mark.asyncio
    async def test_a_single_source_answer_carries_no_comparison_caveats(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "search_hotels", providers=["airbnb"], **STAY)
        assert out["caveats"] == []
        assert [r["provider"] for r in out["providers"]] == [AIRBNB]

    @pytest.mark.asyncio
    async def test_the_billing_note_does_not_claim_one_request(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )
        assert out["api_usage"]["requests_used_by_this_call"] == 2
        assert "own" in out["api_usage"]["note"]


# ── 5. a source the caller cannot use ───────────────────────────────────


class TestSkipped:
    @pytest.mark.asyncio
    async def test_a_403_is_reported_as_not_subscribed_with_a_subscribe_url(self):
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(
                403, json={"message": "You are not subscribed to this API."}
            )
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )

        assert [r["provider"] for r in out["providers"]] == [BOOKING]
        skipped = out["providers_skipped"]
        assert [r["provider"] for r in skipped] == [AIRBNB]
        assert skipped[0]["reason"] == "not_subscribed"
        assert skipped[0]["subscribe_url"] == ota.SUBSCRIBE_URLS[AIRBNB]
        # The other source still answered.
        assert out["result_count"] == 1
        assert out["results"][0]["provider"] == BOOKING

    @pytest.mark.asyncio
    async def test_a_refused_source_is_never_retried_on_our_own_key(self):
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(403, json={"message": "not subscribed"})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(mcp, "search_hotels", providers=["airbnb"], **STAY)
        # One attempt, one key, and that key is the caller's.
        assert len(recorder.to("flightpowers.com")) == 1
        assert recorder.keys_sent == {KEY}

    @pytest.mark.asyncio
    async def test_a_skipped_source_earns_a_caveat_naming_it(self):
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(403, json={"message": "nope"})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )
        assert any("airbnb was not searched" in c for c in out["caveats"])

    @pytest.mark.asyncio
    async def test_no_key_at_all_is_the_keyless_reply_plus_the_named_sources(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key="")
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )
        assert out["needs_api_key"] is True
        assert recorder.requests == []
        assert {r["provider"] for r in out["providers_skipped"]} == {BOOKING, AIRBNB}
        assert all(r["reason"] == "no_key" for r in out["providers_skipped"])


# ── 6. degraded is a named row ──────────────────────────────────────────


class TestDegraded:
    @pytest.mark.asyncio
    async def test_provider_unavailable_is_degraded_not_empty(self):
        """flight_rabbi #472 ships Airbnb OFF: the front answers
        `503 provider_unavailable`. An empty list here would read as "Airbnb
        has nothing", which is the opposite of what happened."""
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(
                503,
                json={
                    "error": {
                        "type": "provider_unavailable",
                        "message": "provider airbnb is not enabled",
                    }
                },
            )
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )

        rows = {r["provider"]: r for r in out["providers"]}
        assert rows[AIRBNB]["search_status"] == "degraded"
        assert rows[AIRBNB]["search_reason"] == "provider_unavailable"
        # Null, never zero: a search that failed does not know there was
        # nothing there.
        assert rows[AIRBNB]["count"] is None
        # The other source still returned its rows.
        assert out["result_count"] == 1
        assert any("could not answer" in c or "did not answer" in c
                   for c in out["caveats"])

    @pytest.mark.asyncio
    async def test_a_200_that_declares_itself_degraded_is_not_an_empty_answer(self):
        """The honest envelope from the lite lane, passed through the front."""
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(
                200,
                json={
                    "properties": [],
                    "search_status": "degraded",
                    "search_reason": "page_unreadable",
                },
            )
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "search_hotels", providers=["airbnb"], **STAY)
        row = out["providers"][0]
        assert row["search_status"] == "degraded"
        assert row["search_reason"] == "page_unreadable"
        assert row["count"] is None
        assert "message" in out

    @pytest.mark.asyncio
    async def test_a_timeout_degrades_that_source_and_keeps_the_other(self):
        def airbnb(request):
            raise httpx.ReadTimeout("too slow", request=request)

        recorder = Recorder(airbnb=airbnb)
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(
            mcp, "search_hotels", providers=["booking", "airbnb"], **STAY
        )
        rows = {r["provider"]: r for r in out["providers"]}
        assert rows[AIRBNB]["search_status"] == "degraded"
        assert rows[AIRBNB]["search_reason"] == "timeout"
        assert out["result_count"] == 1

    @pytest.mark.asyncio
    async def test_an_empty_source_is_an_answer_and_says_so(self):
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(200, json={"properties": []})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "search_hotels", providers=["airbnb"], **STAY)
        row = out["providers"][0]
        assert row["search_status"] == "empty"
        assert row["count"] == 0


# ── 7. booking-only arguments ───────────────────────────────────────────


class TestBookingOnlyArguments:
    @pytest.mark.asyncio
    async def test_filters_on_an_airbnb_only_search_are_refused_not_dropped(self):
        mcp = build_with_upstream(Recorder(), fallback_rapidapi_key=KEY)
        with pytest.raises(ToolError) as exc:
            await call(
                mcp,
                "search_hotels",
                providers=["airbnb"],
                filters=["free_cancellation"],
                **STAY,
            )
        assert "booking" in str(exc.value)

    @pytest.mark.asyncio
    async def test_price_as_seen_from_on_an_airbnb_only_search_is_refused(self):
        mcp = build_with_upstream(Recorder(), fallback_rapidapi_key=KEY)
        with pytest.raises(ToolError):
            await call(
                mcp,
                "search_hotels",
                providers=["airbnb"],
                price_as_seen_from="de",
                **STAY,
            )

    @pytest.mark.asyncio
    async def test_they_reach_booking_and_not_the_front_on_a_mixed_search(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(
            mcp,
            "search_hotels",
            providers=["booking", "airbnb"],
            filters=["gym"],
            price_as_seen_from="de",
            **STAY,
        )
        booking_body = recorder.body("rapidapi.com")
        assert booking_body["filters"] == ["gym"]
        assert booking_body["proxy_country"] == "de"
        front_body = recorder.body("flightpowers.com")
        assert "filters" not in front_body
        assert "proxy_country" not in front_body

    @pytest.mark.asyncio
    async def test_budget_per_night_is_forwarded_for_the_front_to_translate(self):
        """The front maps it onto Airbnb's `price_max` and reports the rewrite.
        Mapping it here as well would apply the ceiling twice."""
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        await call(
            mcp, "search_hotels", providers=["airbnb"], budget_per_night=120, **STAY
        )
        assert recorder.body("flightpowers.com")["budget_per_night"] == 120

    @pytest.mark.asyncio
    async def test_an_unknown_provider_is_rejected_before_a_request_is_spent(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        with pytest.raises(ToolError):
            await call(mcp, "search_hotels", providers=["expedia"], **STAY)
        assert recorder.requests == []


# ── 8. compare_hotel_rates ──────────────────────────────────────────────


class TestCompareToolSurface:
    @pytest.mark.asyncio
    async def test_it_is_registered_on_hotels_and_not_on_flights(self):
        for products, present in (("hotels", True), ("flights", False)):
            mcp = build_with_upstream(Recorder(), products=products)
            async with Client(mcp) as client:
                names = {t.name for t in await client.list_tools()}
            assert ("compare_hotel_rates" in names) is present

    @pytest.mark.asyncio
    async def test_the_annotations_a_directory_reviewer_reads(self):
        mcp = build_with_upstream(Recorder(), products="hotels")
        async with Client(mcp) as client:
            tool = {t.name: t for t in await client.list_tools()}["compare_hotel_rates"]
        assert tool.title == "FlightPowers: compare hotel rates across sources"
        annotations = tool.annotations
        assert annotations.title == tool.title
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
        assert annotations.openWorldHint is True
        # The one that matters: two identical calls give two different
        # answers, and a host entitled to cache would quote a stale rate.
        assert annotations.idempotentHint is False

    @pytest.mark.asyncio
    async def test_every_parameter_carries_a_description(self):
        from src.schema_docs import undocumented_params

        mcp = build_with_upstream(Recorder(), products="hotels")
        async with Client(mcp) as client:
            tool = {t.name: t for t in await client.list_tools()}["compare_hotel_rates"]
        assert undocumented_params(tool.inputSchema) == []

    @pytest.mark.asyncio
    async def test_it_declares_an_output_schema(self):
        mcp = build_with_upstream(Recorder(), products="hotels")
        async with Client(mcp) as client:
            tool = {t.name: t for t in await client.list_tools()}["compare_hotel_rates"]
        schema = tool.outputSchema
        assert schema["required"] == ["providers"]
        assert "providers_skipped" in schema["properties"]
        assert "caveats" in schema["properties"]


class TestCompareRows:
    @pytest.mark.asyncio
    async def test_one_row_per_source_with_the_figures_the_design_asked_for(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", adults=2, currency="usd", **STAY)

        assert out["nights"] == 3
        assert out["currency"] == "USD"
        rows = {r["provider"]: r for r in out["providers"]}
        assert set(rows) == {BOOKING, AIRBNB}

        airbnb = rows[AIRBNB]
        assert airbnb["count"] == 2
        assert airbnb["cheapest_total"] == 423
        assert airbnb["cheapest_name"] == "Villa San Giuseppe"
        assert airbnb["cheapest_link"].endswith("1083378667420950029")
        assert airbnb["median_total"] == 506
        assert airbnb["rating_scale"] == 5
        assert airbnb["taxes_included"] is None
        # Read off the rows, not off the clock when the answer was assembled.
        assert airbnb["retrieved_at"] == "2026-09-09T18:49:23.735Z"

        assert rows[BOOKING]["rating_scale"] == 10
        assert rows[BOOKING]["taxes_included"] is True

    @pytest.mark.asyncio
    async def test_it_returns_the_top_three_rows_per_source(self):
        many = [
            dict(AIRBNB_ROWS[0], name=f"Room {n}", price=100 + n, price_total=100 + n)
            for n in range(6)
        ]
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(200, json={"properties": many})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", providers=["airbnb"], **STAY)
        top = out["providers"][0]["top"]
        assert [r["name"] for r in top] == ["Room 0", "Room 1", "Room 2"]
        assert all(r["provider"] == AIRBNB for r in top)

    @pytest.mark.asyncio
    async def test_rows_without_a_price_are_not_counted_and_not_averaged(self):
        """A source that returned twenty rows and priced twelve reports
        twelve: the other eight are not evidence about what a stay costs."""
        rows = [dict(AIRBNB_ROWS[0]), {"name": "No price published"}]
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(200, json={"properties": rows})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", providers=["airbnb"], **STAY)
        row = out["providers"][0]
        assert row["count"] == 1
        assert row["cheapest_total"] == 423
        assert row["median_total"] == 423

    @pytest.mark.asyncio
    async def test_a_price_string_is_never_parsed_into_a_number(self):
        rows = [{"name": "Label only", "price_string": "US$2,434"}]
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(200, json={"properties": rows})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", providers=["airbnb"], **STAY)
        assert out["providers"][0]["count"] == 0
        assert out["providers"][0]["cheapest_total"] is None

    @pytest.mark.asyncio
    async def test_two_currencies_are_flagged_and_nothing_is_converted(self):
        eur = [dict(row, currency="EUR") for row in AIRBNB_ROWS]
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(200, json={"properties": eur})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", **STAY)

        rows = {r["provider"]: r for r in out["providers"]}
        assert rows[BOOKING]["currency"] == "USD"
        assert rows[AIRBNB]["currency"] == "EUR"
        assert any("not comparable" in c for c in out["caveats"])
        # The totals are the sources' own numbers, untouched.
        assert rows[AIRBNB]["cheapest_total"] == 423
        assert rows[BOOKING]["cheapest_total"] == 2434

    @pytest.mark.asyncio
    async def test_the_scale_and_tax_caveats_ride_on_every_two_source_answer(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", **STAY)
        assert ota.CAVEAT_RATING_SCALE in out["caveats"]
        assert ota.CAVEAT_TAXES in out["caveats"]

    @pytest.mark.asyncio
    async def test_a_skipped_source_is_named_and_counted_nowhere(self):
        recorder = Recorder(
            airbnb=lambda _r: httpx.Response(403, json={"message": "not subscribed"})
        )
        mcp = build_with_upstream(recorder, fallback_rapidapi_key=KEY)
        out = await call(mcp, "compare_hotel_rates", **STAY)
        assert [r["provider"] for r in out["providers"]] == [BOOKING]
        assert out["providers_skipped"][0]["provider"] == AIRBNB
        assert out["providers_skipped"][0]["reason"] == "not_subscribed"
        assert ota.SUBSCRIBE_URLS[AIRBNB] in out["providers_skipped"][0]["subscribe_url"]

    @pytest.mark.asyncio
    async def test_the_keyless_reply_still_conforms_to_the_declared_schema(self):
        recorder = Recorder()
        mcp = build_with_upstream(recorder, fallback_rapidapi_key="")
        out = await call(mcp, "compare_hotel_rates", **STAY)
        assert out["needs_api_key"] is True
        assert out["providers"] == []
        assert {r["provider"] for r in out["providers_skipped"]} == {BOOKING, AIRBNB}
        assert recorder.requests == []
