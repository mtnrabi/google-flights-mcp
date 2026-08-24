"""
The RapidAPI client.

The tests that matter here are the ones that encode why this is not the free
server's LambdaClient with the URL changed: 429 must not be retried, auth and
quota failures must stay distinguishable, and every retry spends someone
else's money.
"""

import httpx
import pytest

from src.rapidapi_client import (
    AuthError,
    QuotaError,
    RapidAPIClient,
    RapidAPIError,
    build_oneway_payload,
    build_roundtrip_payload,
    read_quota,
)

KEY = "test-key-that-is-long-enough-to-pass"
HOST = "google-flights-live-api.p.rapidapi.com"


def make_client(handler) -> RapidAPIClient:
    transport = httpx.MockTransport(handler)
    return RapidAPIClient(
        f"https://{HOST}",
        HOST,
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )


class TestPayloadBuilding:
    def test_none_values_are_omitted_not_nulled(self):
        """sort_type is a strict enum upstream; an explicit null is a 422."""
        payload = build_oneway_payload(
            departure_date="2026-10-01",
            from_airport="TLV",
            to_airport="BUD",
            max_stops=None,
        )
        assert "max_stops" not in payload
        assert payload == {
            "departure_date": "2026-10-01",
            "from_airport": "TLV",
            "to_airport": "BUD",
        }

    def test_false_and_zero_survive(self):
        """Omitting None must not also omit legitimate falsey values."""
        payload = build_oneway_payload(
            departure_date="2026-10-01",
            from_airport="TLV",
            to_airport="BUD",
            max_stops=0,
            use_fallback=False,
        )
        assert payload["max_stops"] == 0
        assert payload["use_fallback"] is False

    def test_roundtrip_carries_both_dates(self):
        payload = build_roundtrip_payload(
            departure_date="2026-10-01",
            return_date="2026-10-08",
            from_airport="TLV",
            to_airport="BUD",
        )
        assert payload["return_date"] == "2026-10-08"


class TestAuthHeaders:
    @pytest.mark.asyncio
    async def test_key_and_host_are_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=[])

        async with make_client(handler) as client:
            await client.search("oneway", {"a": 1}, api_key=KEY)

        assert seen["x-rapidapi-key"] == KEY
        assert seen["x-rapidapi-host"] == HOST

    @pytest.mark.asyncio
    async def test_key_is_per_call_not_per_instance(self):
        """One process serves many subscriptions concurrently. A key held on
        the instance is how one user's key ends up on another user's request."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-rapidapi-key"])
            return httpx.Response(200, json=[])

        async with make_client(handler) as client:
            await client.search("oneway", {}, api_key="key-one-aaaaaaaaaaaa")
            await client.search("oneway", {}, api_key="key-two-bbbbbbbbbbbb")

        assert seen == ["key-one-aaaaaaaaaaaa", "key-two-bbbbbbbbbbbb"]

    @pytest.mark.asyncio
    async def test_endpoint_paths(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json=[])

        async with make_client(handler) as client:
            await client.search("oneway", {}, api_key=KEY)
            await client.search("roundtrip", {}, api_key=KEY)

        assert seen == [
            "/api/google_flights/oneway/v1",
            "/api/google_flights/roundtrip/v1",
        ]


class TestErrorMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failures_raise_auth_error(self, status):
        def handler(_request):
            return httpx.Response(status, json={"message": "Invalid API key"})

        async with make_client(handler) as client:
            with pytest.raises(AuthError, match="Invalid API key"):
                await client.search("oneway", {}, api_key=KEY)

    @pytest.mark.asyncio
    async def test_quota_raises_quota_error(self):
        def handler(_request):
            return httpx.Response(
                429, json={"message": "You have exceeded the MONTHLY quota"}
            )

        async with make_client(handler) as client:
            with pytest.raises(QuotaError, match="MONTHLY quota"):
                await client.search("oneway", {}, api_key=KEY)

    @pytest.mark.asyncio
    async def test_429_is_never_retried(self):
        """On RapidAPI a 429 is an exhausted plan, not backpressure. Every
        retry is another billed request against a quota already spent."""
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(429, json={"message": "quota"})

        async with make_client(handler) as client:
            with pytest.raises(QuotaError):
                await client.search("oneway", {}, api_key=KEY)

        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_auth_failure_is_never_retried(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(401, json={"message": "bad key"})

        async with make_client(handler) as client:
            with pytest.raises(AuthError):
                await client.search("oneway", {}, api_key=KEY)

        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_server_fault_is_retried_then_raises(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(503, text="upstream down")

        async with make_client(handler) as client:
            with pytest.raises(RapidAPIError, match="attempts"):
                await client.search("oneway", {}, api_key=KEY)

        # Two attempts, not three: the caller pays for the extra one.
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_retry_can_succeed(self):
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json=[{"price": "$100"}])

        async with make_client(handler) as client:
            rows = await client.search("oneway", {}, api_key=KEY)

        assert rows == [{"price": "$100"}]

    @pytest.mark.asyncio
    async def test_422_is_not_retried(self):
        """A bad payload is deterministic; retrying just bills it twice."""
        calls = {"n": 0}

        def handler(_request):
            calls["n"] += 1
            return httpx.Response(422, json={"detail": "bad date"})

        async with make_client(handler) as client:
            with pytest.raises(RapidAPIError, match="bad date"):
                await client.search("oneway", {}, api_key=KEY)

        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_upstream_message_is_preserved(self):
        """RapidAPI's own text already names the fix; do not replace it."""
        def handler(_request):
            return httpx.Response(
                403, json={"message": "You are not subscribed to this API."}
            )

        async with make_client(handler) as client:
            with pytest.raises(AuthError) as excinfo:
                await client.search("oneway", {}, api_key=KEY)

        assert "not subscribed" in str(excinfo.value)


class TestResponseParsing:
    @pytest.mark.asyncio
    async def test_empty_list_is_a_result_not_an_error(self):
        async with make_client(lambda _r: httpx.Response(200, json=[])) as client:
            assert await client.search("oneway", {}, api_key=KEY) == []

    @pytest.mark.asyncio
    async def test_enveloped_shape_is_tolerated(self):
        def handler(_request):
            return httpx.Response(200, json={"results": [{"price": "$1"}]})

        async with make_client(handler) as client:
            assert await client.search("oneway", {}, api_key=KEY) == [{"price": "$1"}]

    @pytest.mark.asyncio
    async def test_non_json_body_raises(self):
        def handler(_request):
            return httpx.Response(200, text="<html>gateway</html>")

        async with make_client(handler) as client:
            with pytest.raises(RapidAPIError, match="non-JSON"):
                await client.search("oneway", {}, api_key=KEY)


class TestQuotaHeaders:
    def test_read_quota_parses_all_three(self):
        response = httpx.Response(
            200,
            headers={
                "x-ratelimit-requests-limit": "1000",
                "x-ratelimit-requests-remaining": "987",
                "x-ratelimit-requests-reset": "432000",
            },
        )
        assert read_quota(response) == {
            "plan_requests_limit": 1000,
            "plan_requests_remaining": 987,
            "plan_seconds_until_reset": 432000,
        }

    def test_absent_headers_produce_an_empty_dict(self):
        """These are informational. A gateway that stops sending them must
        not break a search."""
        assert read_quota(httpx.Response(200)) == {}

    def test_unparseable_values_are_skipped_not_fatal(self):
        response = httpx.Response(
            200, headers={"x-ratelimit-requests-remaining": "unlimited"}
        )
        assert read_quota(response) == {}

    @pytest.mark.asyncio
    async def test_quota_sink_is_filled_on_success(self):
        def handler(_request):
            return httpx.Response(
                200,
                json=[],
                headers={"x-ratelimit-requests-remaining": "42"},
            )

        sink: dict[str, int] = {}
        async with make_client(handler) as client:
            await client.search("oneway", {}, api_key=KEY, quota_sink=sink)

        assert sink["plan_requests_remaining"] == 42

    @pytest.mark.asyncio
    async def test_quota_sink_is_filled_on_quota_error_too(self):
        """The 429 response is the one that carries the most useful number."""
        def handler(_request):
            return httpx.Response(
                429,
                json={"message": "exceeded"},
                headers={
                    "x-ratelimit-requests-remaining": "0",
                    "x-ratelimit-requests-limit": "100",
                },
            )

        sink: dict[str, int] = {}
        async with make_client(handler) as client:
            with pytest.raises(QuotaError):
                await client.search("oneway", {}, api_key=KEY, quota_sink=sink)

        assert sink == {"plan_requests_remaining": 0, "plan_requests_limit": 100}


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_use_outside_context_is_an_error_not_a_crash(self):
        client = RapidAPIClient(f"https://{HOST}", HOST)
        with pytest.raises(RapidAPIError, match="outside its async context"):
            await client.search("oneway", {}, api_key=KEY)

    @pytest.mark.asyncio
    async def test_a_handed_in_client_is_not_closed(self):
        """server.py shares one process-wide pool across every request; if the
        per-request wrapper closed it, the second request would fail."""
        shared = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=[]))
        )
        async with RapidAPIClient(f"https://{HOST}", HOST, client=shared):
            pass
        assert not shared.is_closed
        await shared.aclose()
