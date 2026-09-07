"""
One codebase, two products: no string may name the wrong one.

This file exists because the same copy-paste defect has now been found four
times, each time in production, each time in a different string:

1. the hotels deployment introduced itself as "Real-time Google Flights
   search" (`instructions`);
2. its keyless callers were sent to the flights RapidAPI listing
   (`SIGNUP_URL`);
3. its 403 handler read "Subscribe to the Booking Live API at <flights URL>";
4. its keyless reply said the search was "billed to the caller's own Google
   Flights API subscription" -- a bill the caller cannot have, for a product
   they did not buy (`credentials.missing_key_message`).

Each was fixed on its own. Fixing them one at a time is what let there be a
fourth, so this file stops asserting individual sentences and asserts the
invariant instead: **collect every string a deployment can put in front of a
caller, and check that none of them names the other product's API or listing.**
A fifth instance fails here rather than in a user's chat window.

"In front of a caller" is deliberately wide. `instructions` reaches a model
before it has called anything; tool descriptions and titles reach it in
`tools/list`; the keyless, auth-failure and quota replies are written to be
read aloud to a human; `api_usage.note` rides on every successful response;
and `/`, `/health`, `/privacy`, `/terms` and `/support` are what a directory
reviewer opens. All of them are in scope here.

What is NOT asserted is a blanket ban on the other product's *words*. The
hotels 403 says "a flights-only subscription does not cover hotel search",
which is both correct and the most useful sentence in it. The invariant is
narrower and sharper: never name the other product's upstream API, and never
quote the other product's RapidAPI listing.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from fastmcp import Client

from src.credentials import missing_key_message
from src.legal import index_html, render_document, support_html
from src.server import (
    BILLING_UNIT_NOTES,
    UPSTREAM_API_NAMES,
    build_instructions,
    upstream_api_name,
)
from src.settings import DEFAULT_SIGNUP_URLS
from tests.test_server import KEY, build_with_upstream, make_settings

# The two facts that must never cross products: the name of the upstream API a
# caller's key has to be subscribed to, and the listing where they buy it.
FOREIGN = {
    "flights": ("Booking Live API", DEFAULT_SIGNUP_URLS["hotels"], "booking-live-api"),
    "hotels": (
        "Google Flights Live API",
        DEFAULT_SIGNUP_URLS["flights"],
        "google-flights-live-api",
        # The exact phrase of defect 4, kept as its own check so the failure
        # message says which bug came back.
        "Google Flights API subscription",
    ),
}

OWN_API = {"flights": "Google Flights Live API", "hotels": "Booking Live API"}


def assert_no_foreign_product(text: str, products: str, where: str) -> None:
    """`text` is shown by a `products` deployment; fail if it sells the other."""
    for phrase in FOREIGN[products]:
        assert phrase not in text, f"{where} on the {products} deployment names {phrase!r}"


# ── the strings themselves ───────────────────────────────────────────────


def server_for(products: str):
    return build_with_upstream(
        lambda _r: httpx.Response(200, json=[]),
        products=products,
        signup_url=DEFAULT_SIGNUP_URLS[products],
    )


def listed_tools(products: str) -> list[dict]:
    mcp = server_for(products)

    async def _list():
        return await mcp._list_tools()

    return [json.loads(t.to_mcp_tool().model_dump_json()) for t in anyio.run(_list)]


async def _get(mcp, path: str) -> str:
    transport = httpx.ASGITransport(app=mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        response = await http.get(path)
        return response.text


class TestModelFacingCopy:
    """Everything a model reads before and while it calls a tool."""

    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_instructions_sell_only_this_deployments_product(self, products):
        """`instructions` is the one piece of prose every client reads before
        it has called anything, and for many models the only thing that
        decides whether the server is reached for at all. Defect 1."""
        text = build_instructions(
            make_settings(products=products, signup_url=DEFAULT_SIGNUP_URLS[products])
        )
        assert_no_foreign_product(text, products, "instructions")
        assert OWN_API[products] in text
        assert DEFAULT_SIGNUP_URLS[products] in text

    def test_the_combined_deployment_names_both_listings(self):
        """"both" sells two subscriptions, so quoting one URL for two named
        APIs sends whichever half the caller wanted second to the wrong
        Subscribe page."""
        settings = make_settings(products="both")
        text = build_instructions(settings)
        # SIGNUP_URL is this deployment's own listing and still wins for the
        # product it covers; the hotels half gets the listing it needs.
        assert settings.signup_url in text
        assert DEFAULT_SIGNUP_URLS["hotels"] in text

        default = build_instructions(
            make_settings(products="both", signup_url=DEFAULT_SIGNUP_URLS["both"])
        )
        assert DEFAULT_SIGNUP_URLS["flights"] in default
        assert DEFAULT_SIGNUP_URLS["hotels"] in default

    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_tool_descriptions_and_titles_stay_in_product(self, products):
        tools = listed_tools(products)
        assert tools, f"{products} deployment exposes no tools"
        for tool in tools:
            for field in ("title", "description"):
                assert_no_foreign_product(
                    tool.get(field) or "", products, f"{tool['name']}.{field}"
                )
            annotations = tool.get("annotations") or {}
            assert_no_foreign_product(
                annotations.get("title") or "",
                products,
                f"{tool['name']}.annotations.title",
            )
            for name, schema in (
                tool.get("inputSchema", {}).get("properties", {}).items()
            ):
                assert_no_foreign_product(
                    schema.get("description") or "",
                    products,
                    f"{tool['name']}({name})",
                )


class TestKeylessCallerIsSoldTheRightSubscription:
    """Defects 2 and 4: what a caller with no key is told to go and buy."""

    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_message_names_this_products_api_and_listing(self, products):
        text = missing_key_message(
            DEFAULT_SIGNUP_URLS[products], upstream_api_name(products)
        )
        assert f"{OWN_API[products]} subscription" in text
        assert_no_foreign_product(text, products, "missing_key_message")

    @pytest.mark.parametrize(
        "products,tool,args",
        [
            (
                "flights",
                "search_oneway_flights",
                {
                    "from_airport": "TLV",
                    "to_airport": "BUD",
                    "departure_date": "2026-09-20",
                },
            ),
            (
                "hotels",
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-05-01",
                    "checkout_date": "2026-05-04",
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_over_the_wire_with_no_key(self, products, tool, args):
        """Asserted through a real tool call, because the defect was never in
        the helper -- it was in which arguments the call site passed it."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products=products,
            signup_url=DEFAULT_SIGNUP_URLS[products],
            # No server-side fallback: this is a genuinely keyless caller.
            fallback_rapidapi_key="",
        )
        async with Client(mcp) as client:
            result = (await client.call_tool(tool, args)).structured_content

        assert result["needs_api_key"] is True
        assert result["signup_url"] == DEFAULT_SIGNUP_URLS[products]
        assert f"{OWN_API[products]} subscription" in result["message"]
        assert_no_foreign_product(result["message"], products, f"{tool} keyless reply")

    @pytest.mark.asyncio
    async def test_combined_deployment_sends_hotels_callers_to_the_hotels_listing(
        self,
    ):
        """The "both" deployment has one SIGNUP_URL and it is the flights
        listing, so this is the case the per-product resolution exists for."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products="both",
            fallback_rapidapi_key="",
        )
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content

        assert result["signup_url"] == DEFAULT_SIGNUP_URLS["hotels"]
        assert_no_foreign_product(
            result["message"], "hotels", "combined-deployment keyless hotels reply"
        )


class TestFailureRepliesSellTheRightSubscription:
    @pytest.mark.asyncio
    async def test_hotels_403_quotes_the_hotels_listing(self):
        """Defect 3. It read "Subscribe to the Booking Live API at <flights
        URL>" -- self-contradicting, and the URL is the half a user acts on."""

        def handler(_r):
            return httpx.Response(403, json={"message": "You are not subscribed."})

        mcp = build_with_upstream(handler, products="both", fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content
        assert result["needs_api_key"] is True
        message = result["message"]
        assert "Booking Live API" in message
        assert DEFAULT_SIGNUP_URLS["hotels"] in message
        assert DEFAULT_SIGNUP_URLS["flights"] not in message
        howto = result["how_to_get_a_key"]
        assert howto["signup_url"] == DEFAULT_SIGNUP_URLS["hotels"]
        assert DEFAULT_SIGNUP_URLS["flights"] not in json.dumps(howto)

    @pytest.mark.asyncio
    async def test_flights_401_quotes_the_flights_listing(self):
        def handler(_r):
            return httpx.Response(401, json={"message": "Invalid API key."})

        mcp = build_with_upstream(
            handler,
            products="flights",
            signup_url=DEFAULT_SIGNUP_URLS["flights"],
            fallback_rapidapi_key=KEY,
        )
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "BUD",
                        "departure_date": "2026-09-20",
                    },
                )
            ).structured_content
        assert result["needs_api_key"] is True
        assert_no_foreign_product(result["message"], "flights", "flights 401 reply")


class TestSpendReportingDescribesTheRightBilling:
    """`api_usage.note` rides on every successful response and is the thing a
    model repeats when asked "what did that cost me?"."""

    def test_the_two_products_bill_differently(self):
        assert "combination" in BILLING_UNIT_NOTES["flights"]
        assert "no fan-out" in BILLING_UNIT_NOTES["hotels"]

    @pytest.mark.asyncio
    async def test_a_hotel_search_does_not_claim_a_fan_out(self):
        """A hotel call is exactly one upstream request. Telling the caller
        that "each date and destination combination is one billed request"
        describes a fan-out these tools do not have."""

        def handler(_r):
            return httpx.Response(
                200,
                json={"properties": [{"name": "Hotel Artemide", "price": 210}]},
                headers={"x-ratelimit-requests-remaining": "97"},
            )

        mcp = build_with_upstream(handler, products="hotels", fallback_rapidapi_key=KEY)
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content

        note = result["api_usage"]["note"]
        assert BILLING_UNIT_NOTES["hotels"] in note
        assert "date and destination combination" not in note

    @pytest.mark.asyncio
    async def test_a_flight_search_still_explains_the_fan_out(self):
        def handler(_r):
            return httpx.Response(
                200,
                json=[
                    {
                        "price": "$209",
                        "price_as_number": 209,
                        "buy_link": "https://google.test/a",
                    }
                ],
            )

        mcp = build_with_upstream(
            handler, products="flights", fallback_rapidapi_key=KEY
        )
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "BUD",
                        "departure_date": "2026-09-20",
                    },
                )
            ).structured_content
        assert BILLING_UNIT_NOTES["flights"] in result["api_usage"]["note"]


class TestPublicPagesStayInProduct:
    """What a directory reviewer opens. A policy that names the wrong upstream
    processor is not a policy for that service."""

    @pytest.mark.parametrize("products", ("flights", "hotels"))
    @pytest.mark.parametrize("document", ("privacy", "terms"))
    def test_policies(self, products, document):
        body = render_document(document, products)
        assert body
        assert_no_foreign_product(body, products, f"{document} page")
        assert UPSTREAM_API_NAMES[products] in body

    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_support_and_index(self, products):
        signup = DEFAULT_SIGNUP_URLS[products]
        assert_no_foreign_product(support_html(products), products, "support page")
        assert signup in support_html(products)
        index = index_html(products, "https://mcp.test", signup)
        assert_no_foreign_product(index, products, "index page")

    def test_the_combined_deployment_discloses_both_upstreams(self):
        """It forwards to two APIs and returns two brands' data, so naming one
        of each is a policy for half the server."""
        for document in ("privacy", "terms"):
            body = render_document(document, "both")
            assert "Google Flights Live API" in body
            assert "Booking Live API" in body
            assert "Booking.com" in body

    @pytest.mark.parametrize("products", ("flights", "hotels", "both"))
    def test_the_hotel_policies_never_describe_a_flight_fan_out(self, products):
        """The paragraph describing "a date range and a list of destinations"
        expanded into 30 searches was fixed for every deployment, so it stated
        a cap and a shape the hotel tools do not have."""
        body = render_document("privacy", products)
        has_flight_tools = products in ("flights", "both")
        assert ("list of destinations" in body) is has_flight_tools

    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ("flights", "hotels"))
    @pytest.mark.parametrize("path", ("/", "/health", "/privacy", "/terms", "/support"))
    async def test_served_routes(self, products, path):
        """Rendered through the running app, not the renderer, because a route
        can be wired to the wrong product without the renderer being wrong."""
        body = await _get(server_for(products), path)
        assert_no_foreign_product(body, products, f"{path} on {products}")
