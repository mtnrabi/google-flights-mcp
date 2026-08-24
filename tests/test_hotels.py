"""
Hotel search: the second product on the same server and the same key.

The two behaviours worth guarding are the ones a caller cannot see going
wrong. A typo in `filters` must not silently return unfiltered results, and a
flights-only subscriber hitting a hotel tool must be told to subscribe rather
than shown an empty list.
"""

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.hotels_client import (
    VALID_FILTERS,
    AuthError,
    HotelsClient,
    QuotaError,
    RapidAPIError,
    build_hotel_by_name_payload,
    build_search_payload,
    unknown_filters,
)
from tests.test_server import build_with_upstream

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

PROPERTY = {
    "name": "Kremlin Palace",
    "price_string": "US$2,434",
    "price": 2434,
    "review_score": 8.4,
    "review_count": 701,
    "room_type": "Superior Double or Twin Room",
    "link": "https://www.booking.com/hotel/tr/kremlin-palace.html",
}


class TestFilterValidation:
    def test_known_filters_pass(self):
        assert unknown_filters(["free_cancellation", "gym"]) == []

    def test_typo_is_caught(self):
        """The API's behaviour for an unknown filter is undocumented, and
        silently returning unfiltered results is the worst outcome: someone
        monitoring 'free cancellation only' would get everything and not
        know."""
        assert unknown_filters(["free_cancelation"]) == ["free_cancelation"]

    def test_none_and_empty_are_fine(self):
        assert unknown_filters(None) == []
        assert unknown_filters([]) == []

    def test_the_documented_set_is_complete(self):
        assert len(VALID_FILTERS) == 24
        for name in ("free_cancellation", "adults_only", "stars_5", "sauna"):
            assert name in VALID_FILTERS


class TestPayloads:
    def test_required_only(self):
        got = build_search_payload(
            destination="Rome", checkin_date="2026-05-01", checkout_date="2026-05-04"
        )
        assert got == {
            "destination": "Rome",
            "checkin_date": "2026-05-01",
            "checkout_date": "2026-05-04",
        }

    def test_none_values_are_dropped_never_sent_as_null(self):
        got = build_search_payload(
            destination="Rome",
            checkin_date="2026-05-01",
            checkout_date="2026-05-04",
            adults=None,
            currency=None,
        )
        assert "adults" not in got
        assert "currency" not in got

    def test_proxy_country_is_carried_through(self):
        """The rate-parity feature. If this silently stopped being sent the
        results would still look plausible, just priced from the wrong
        market."""
        got = build_search_payload(
            destination="Rome",
            checkin_date="2026-05-01",
            checkout_date="2026-05-04",
            proxy_country="DE",
        )
        assert got["proxy_country"] == "DE"

    def test_empty_filter_list_is_omitted(self):
        got = build_search_payload(
            destination="Rome",
            checkin_date="2026-05-01",
            checkout_date="2026-05-04",
            filters=[],
        )
        assert "filters" not in got

    def test_hotel_by_name_payload(self):
        got = build_hotel_by_name_payload(
            hotel_name="Boffenigo",
            checkin_date="2026-05-01",
            checkout_date="2026-05-04",
            proxy_country="US",
        )
        assert got["hotel_name"] == "Boffenigo"
        assert got["proxy_country"] == "US"


class TestHotelsClient:
    @pytest.mark.asyncio
    async def test_sends_key_and_host(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"properties": [PROPERTY]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                body = await client.call("search", {"destination": "Rome"}, api_key=KEY)

        assert seen["x-rapidapi-key"] == KEY
        assert seen["x-rapidapi-host"] == "booking-live-api.p.rapidapi.com"
        assert seen["url"].endswith("/search")
        assert body["properties"] == [PROPERTY]

    @pytest.mark.asyncio
    async def test_quota_headers_are_read(self):
        def handler(_r):
            return httpx.Response(
                200,
                json={"properties": []},
                headers={"x-ratelimit-requests-remaining": "19997"},
            )

        quota: dict[str, int] = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                await client.call("search", {}, api_key=KEY, quota_sink=quota)
        assert quota["plan_requests_remaining"] == 19997

    @pytest.mark.asyncio
    async def test_403_is_an_auth_error(self):
        def handler(_r):
            return httpx.Response(403, json={"message": "You are not subscribed"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                with pytest.raises(AuthError):
                    await client.call("search", {}, api_key=KEY)

    @pytest.mark.asyncio
    async def test_429_is_never_retried(self):
        """The caller's plan is already spent; a retry bills them again for a
        request that cannot succeed."""
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(429, json={"message": "quota"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                with pytest.raises(QuotaError):
                    await client.call("search", {}, api_key=KEY)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_server_fault_is_retried_once_then_succeeds(self):
        calls = []

        def handler(_r):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"properties": [PROPERTY]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                body = await client.call("search", {}, api_key=KEY)
        assert len(calls) == 2
        assert body["properties"] == [PROPERTY]

    @pytest.mark.asyncio
    async def test_retries_are_bounded_at_two(self):
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(500, text="boom")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsClient(client=http) as client:
                with pytest.raises(RapidAPIError):
                    await client.call("search", {}, api_key=KEY)
        assert len(calls) == 2


class TestHotelTools:
    @pytest.mark.asyncio
    async def test_search_returns_properties_and_usage(self):
        def handler(_r):
            return httpx.Response(
                200,
                json={"properties": [PROPERTY], "applied_filters": ["gym"]},
                headers={"x-ratelimit-requests-remaining": "19990"},
            )

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Antalya",
                    "checkin_date": "2026-05-01",
                    "checkout_date": "2026-05-10",
                },
            )
        data = out.data
        assert data["result_count"] == 1
        assert data["results"][0]["name"] == "Kremlin Palace"
        assert data["applied_filters"] == ["gym"]
        assert data["api_usage"]["plan_requests_remaining"] == 19990

    @pytest.mark.asyncio
    async def test_single_property_response_is_normalised_to_a_list(self):
        """`/hotel_by_name` answers with one object where `/search` answers
        with a list. A model should not have to branch on that."""

        def handler(_r):
            return httpx.Response(200, json=PROPERTY)

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            out = await client.call_tool(
                "find_hotel_by_name",
                {
                    "hotel_name": "Kremlin Palace",
                    "checkin_date": "2026-05-01",
                    "checkout_date": "2026-05-10",
                },
            )
        assert out.data["result_count"] == 1
        assert out.data["results"][0]["name"] == "Kremlin Palace"

    @pytest.mark.asyncio
    async def test_bad_filter_is_rejected_before_spending_a_request(self):
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(200, json={"properties": []})

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                        "filters": ["free_cancelation"],
                    },
                )
        assert calls == [], "a typo must not cost the caller a billed request"

    @pytest.mark.asyncio
    async def test_no_key_spends_nothing_and_explains(self):
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(200, json={"properties": []})

        mcp = build_with_upstream(handler, fallback_rapidapi_key="")
        async with Client(mcp) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-05-01",
                    "checkout_date": "2026-05-04",
                },
            )
        assert out.data["needs_api_key"] is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_unsubscribed_key_says_to_subscribe_not_no_results(self):
        """A flights-only subscriber must not be shown an empty hotel list --
        that reads as 'no hotels', not 'you have not bought this'."""

        def handler(_r):
            return httpx.Response(403, json={"message": "You are not subscribed"})

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
        assert "Subscribe" in str(exc.value)

    @pytest.mark.asyncio
    async def test_proxy_country_reaches_the_upstream_body(self):
        sent = {}

        def handler(request: httpx.Request):
            import json as _json

            sent.update(_json.loads(request.content))
            return httpx.Response(200, json={"properties": []})

        mcp = build_with_upstream(handler, fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-05-01",
                    "checkout_date": "2026-05-04",
                    "price_as_seen_from": "DE",
                },
            )
        assert sent["proxy_country"] == "DE"
