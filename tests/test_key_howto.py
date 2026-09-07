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

1. `credentials.key_howto_steps` / `key_howto_block` / `key_howto_tail` --
   the single source the other three read from.
2. The server `instructions` string (`build_server`/`build_instructions`).
3. The tail of every tool description.
4. The `how_to_get_a_key` object on a missing-key or invalid-key tool result,
   structured so a model can act on it rather than re-parsing `message`.

Nothing here touches the success path or credential resolution order --
`TestGatewayOwnKeyIsNotMistakenForOurs` in test_credentials.py is the guard
for that and is untouched by this change.
"""

from __future__ import annotations

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

    def test_tail_is_the_short_version_of_the_same_facts(self):
        tail = key_howto_tail("https://rapidapi.test/x", "Example API")
        assert isinstance(tail, str)
        assert_carries_the_three_steps(tail, "https://rapidapi.test/x")
        assert "Example API" in tail


class TestMissingKeyMessageStillHoldsAllThree:
    """`missing_key_message` predates this task; it must not lose ground."""

    def test_no_key_message_carries_the_three_steps(self):
        from src.credentials import missing_key_message

        text = missing_key_message("https://rapidapi.test/x", "Example API")
        assert_carries_the_three_steps(text, "https://rapidapi.test/x")


class TestInstructionsCarryTheTail:
    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_single_product_instructions(self, products):
        settings = make_settings(
            products=products, signup_url=DEFAULT_SIGNUP_URLS[products]
        )
        text = build_instructions(settings)
        assert_carries_the_three_steps(text, DEFAULT_SIGNUP_URLS[products])

    def test_combined_deployment_instructions(self):
        settings = make_settings(
            products="both", signup_url=DEFAULT_SIGNUP_URLS["both"]
        )
        text = build_instructions(settings)
        # "both" concatenates two listings into the one signup slot; the
        # shared invariant (three ways, api_usage) still has to hold, and
        # both listings must be reachable in the text.
        for way in THREE_WAYS:
            assert way in text
        assert "api_usage" in text
        assert DEFAULT_SIGNUP_URLS["flights"] in text
        assert DEFAULT_SIGNUP_URLS["hotels"] in text


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


class TestToolDescriptionsCarryTheTail:
    @pytest.mark.parametrize("products", ("flights", "hotels"))
    def test_every_tool_on_this_deployment_carries_it(self, products):
        tools = listed_tools(products)
        assert tools
        expected_signup = DEFAULT_SIGNUP_URLS[products]
        for tool in tools:
            description = tool["description"]
            assert_carries_the_three_steps(description, expected_signup)

    def test_combined_deployment_each_tool_names_only_its_own_listing(self):
        """A "both" deployment still routes each tool to its own product's
        listing -- a flights tool must not carry the hotels URL and back,
        matching the invariant test_product_copy.py enforces elsewhere for
        this deployment shape."""
        tools = {t["name"]: t for t in listed_tools("both")}
        flights_tools = ("search_oneway_flights", "search_roundtrip_flights")
        hotels_tools = ("search_hotels", "find_hotel_by_name")

        for name in flights_tools:
            description = tools[name]["description"]
            assert_carries_the_three_steps(
                description, DEFAULT_SIGNUP_URLS["flights"]
            )
            assert DEFAULT_SIGNUP_URLS["hotels"] not in description

        for name in hotels_tools:
            description = tools[name]["description"]
            assert_carries_the_three_steps(
                description, DEFAULT_SIGNUP_URLS["hotels"]
            )
            assert DEFAULT_SIGNUP_URLS["flights"] not in description


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
