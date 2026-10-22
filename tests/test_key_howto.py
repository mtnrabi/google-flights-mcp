"""
The self-serve path for a caller who reaches this PAID server with no key, or
with a key that gets rejected.

This server holds no upstream credential of its own (see credentials.py): a
caller who shows up without one, or whose key is not subscribed to the right
listing, is stuck until they learn three things -- where to buy a key, the
three ways to hand it to this server, and that the spend is theirs. Before
this file, that path lived only inside `missing_key_message`, reachable only
after a call had already failed once.

So the same three steps now ride in four places, and this file is what keeps
them from drifting apart the way the flights/hotels copy did before (see
test_product_copy.py's docstring for that history):

1. `credentials.key_howto_steps` / `key_howto_block` -- the single source
   the refusals read from.
2. (until 2026-10-22) the server `instructions` string. Now the opposite is
   asserted: `key_howto_tail` names /connect (or says a search without a key
   answers with the steps) and carries no listing URL and no header /
   `?rapidapi_key=` how-to, because a directory reviewer reads instructions
   and the server is submitted as OAuth.
3. (until 2026-10-22) the tail of every tool description -- now the
   opposite is asserted: no description carries a listing or a price.
4. The `how_to_get_a_key` object on a missing-key or invalid-key tool result,
   structured so a model can act on it rather than re-parsing `message`.

Nothing here touches the success path or credential resolution order --
`TestGatewayOwnKeyIsNotMistakenForOurs` in test_credentials.py is the guard
for that and is untouched by this change.
"""

from __future__ import annotations

import re

import anyio
import httpx
import pytest
from fastmcp import Client

from src.credentials import key_howto_block, key_howto_steps, key_howto_tail
from src.server import build_instructions, upstream_api_name
from src.settings import DEFAULT_SIGNUP_URLS
from tests.test_server import KEY, build_with_upstream, make_settings

FLIGHTS_API = upstream_api_name("flights")
HOTELS_API = upstream_api_name("hotels")

THREE_WAYS = ("x-rapidapi-key", "?rapidapi_key=", "API key field")


def assert_carries_the_three_steps(text: str, signup_url: str) -> None:
    """(1) where to get a key, (2) the three ways to pass it, (3) usage
    counts against the caller's own plan -- the invariant every surface in
    this file has to satisfy."""
    assert signup_url in text
    for way in THREE_WAYS:
        assert way in text, f"missing {way!r} in: {text!r}"
    assert "api_usage" in text
    assert "own RapidAPI plan" in text


class TestKeyHowtoSteps:
    """The single source of truth in credentials.py."""

    def test_three_steps_cover_get_pass_and_spend(self):
        steps = key_howto_steps("https://rapidapi.test/x", "Example API")
        assert len(steps) == 3
        assert_carries_the_three_steps("\n".join(steps), "https://rapidapi.test/x")
        # Numbered, so a model can quote them back in order.
        assert steps[0].startswith("1.")
        assert steps[1].startswith("2.")
        assert steps[2].startswith("3.")

    def test_block_is_structured_not_prose(self):
        block = key_howto_block("https://rapidapi.test/x", "Example API")
        assert block["signup_url"] == "https://rapidapi.test/x"
        assert block["how"] == key_howto_steps("https://rapidapi.test/x", "Example API")

    def test_tail_names_connect_and_no_passing_method(self):
        tail = key_howto_tail("Example API", "https://srv.test/connect")
        assert "https://srv.test/connect" in tail
        for way in THREE_WAYS:
            assert way not in tail
        assert "rapidapi.com" not in tail

    def test_tail_without_connect_points_at_the_refusal(self):
        tail = key_howto_tail("Example API")
        assert "Example API" in tail
        assert "steps" in tail
        for way in THREE_WAYS:
            assert way not in tail


class TestMissingKeyMessageStillHoldsAllThree:
    """`missing_key_message` predates this task; it must not lose ground."""

    def test_no_key_message_carries_the_three_steps(self):
        from src.credentials import missing_key_message

        text = missing_key_message("https://rapidapi.test/x", "Example API")
        assert_carries_the_three_steps(text, "https://rapidapi.test/x")


class TestInstructionsCarryNoKeyHowto:
    @pytest.mark.parametrize("products", ("flights", "hotels", "both"))
    def test_no_listing_url_and_no_passing_method(self, products):
        settings = make_settings(
            products=products, signup_url=DEFAULT_SIGNUP_URLS[products]
        )
        text = build_instructions(settings)
        for way in THREE_WAYS:
            assert way not in text
        assert "rapidapi.com" not in text
        # What a call spent is still said, in `_SPEND`.
        assert "api_usage" in text


def listed_tools(products: str) -> list[dict]:
    mcp = build_with_upstream(
        lambda _r: httpx.Response(200, json=[]),
        products=products,
        signup_url=DEFAULT_SIGNUP_URLS[products],
    )

    async def _list():
        return await mcp._list_tools()

    import json

    return [json.loads(t.to_mcp_tool().model_dump_json()) for t in anyio.run(_list)]


class TestToolDescriptionsCarryNoPromotion:
    """Since 2026-10-22 a tool description says what the tool does, its
    inputs and its limits -- never a signup link, a price or the get-a-key
    how-to. Claude's directory review reads those as promotion inside a tool
    description; the how-to lives in `instructions` (above), the 401 body,
    /connect and every refusal (below)."""

    PRICE = re.compile(r"\$\s?\d")
    BANNED = ("rapidapi.com", "free tier", "billed to", "/pricing", "PRO is")

    @pytest.mark.parametrize("products", ("flights", "hotels", "both"))
    def test_no_description_names_a_listing_or_a_price(self, products):
        tools = listed_tools(products)
        assert tools
        for tool in tools:
            description = tool["description"]
            for banned in self.BANNED:
                assert banned not in description, (tool["name"], banned)
            assert not self.PRICE.search(description), tool["name"]
            # ...and the descriptions still describe the tool.
            assert "Returns" in description
            assert len(description) > 100


class TestHowToGetAKeyOnMissingOrInvalidKey:
    """The structured object added to every tool's missing/invalid-key
    result, on both products."""

    @pytest.mark.asyncio
    async def test_flights_no_key(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), fallback_rapidapi_key=""
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "BUD",
                        "departure_date": "2026-09-20",
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        howto = out["how_to_get_a_key"]
        assert howto["signup_url"] == "https://rapidapi.test/google-flights"
        assert howto["how"] == key_howto_steps(howto["signup_url"], FLIGHTS_API)

    @pytest.mark.asyncio
    async def test_flights_invalid_key(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(401, json={"message": "Invalid API key"}),
            fallback_rapidapi_key=KEY,
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "search_oneway_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "BUD",
                        "departure_date": "2026-09-20",
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        howto = out["how_to_get_a_key"]
        assert len(howto["how"]) == 3
        assert howto["signup_url"] == "https://rapidapi.test/google-flights"

    @pytest.mark.asyncio
    async def test_roundtrip_shares_the_same_object(self):
        """`search_roundtrip_flights` shares `_run` with the one-way tool;
        this pins that the structured object rides along there too."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), fallback_rapidapi_key=""
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "search_roundtrip_flights",
                    {
                        "from_airport": "TLV",
                        "to_airport": "BUD",
                        "departure_date": "2026-09-20",
                        "nights": 5,
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        assert "how_to_get_a_key" in out

    @pytest.mark.asyncio
    async def test_hotels_no_key(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json={"properties": []}),
            products="hotels",
            signup_url=DEFAULT_SIGNUP_URLS["hotels"],
            fallback_rapidapi_key="",
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        howto = out["how_to_get_a_key"]
        assert howto["signup_url"] == DEFAULT_SIGNUP_URLS["hotels"]
        assert howto["how"] == key_howto_steps(howto["signup_url"], HOTELS_API)

    @pytest.mark.asyncio
    async def test_hotels_invalid_key(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                403, json={"message": "You are not subscribed"}
            ),
            products="hotels",
            signup_url=DEFAULT_SIGNUP_URLS["hotels"],
            fallback_rapidapi_key=KEY,
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        howto = out["how_to_get_a_key"]
        assert len(howto["how"]) == 3
        assert howto["signup_url"] == DEFAULT_SIGNUP_URLS["hotels"]

    @pytest.mark.asyncio
    async def test_find_hotel_by_name_no_key(self):
        """The second hotel tool, not only `search_hotels`."""
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json={}),
            products="hotels",
            signup_url=DEFAULT_SIGNUP_URLS["hotels"],
            fallback_rapidapi_key="",
        )
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "find_hotel_by_name",
                    {
                        "hotel_name": "Hotel Artemide",
                        "checkin_date": "2026-05-01",
                        "checkout_date": "2026-05-04",
                    },
                )
            ).structured_content
        assert out["needs_api_key"] is True
        assert "how_to_get_a_key" in out
