"""Directory fixes round 3 (2026-10-22, Claude directory review NO-GO 4 and 5).

4. `search_hotels` offered Airbnb through a `providers` argument while the api
   front answers it `provider_unavailable`. Claude reads the input schema as
   well as the description, so the source must be gone from the whole tool
   surface, not only from one paragraph.
5. The server `instructions` named the RapidAPI listing URL and the header /
   `?rapidapi_key=` options. A sales link in model context reads as
   promotion, and the server is submitted as OAuth. What stays: the sign-in
   allowance and /connect. The 401 body, /connect and the missing-key refusal
   keep the full how-to.

Plus: no comparative or superlative claim ("which no other travel tool here
can do", "ad-free") in any tool text or in the instructions. The key /connect
stores is named for what it is, a RapidAPI key, the same word /connect uses.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from fastmcp import Client

from src.server import build_instructions
from src.settings import DEFAULT_SIGNUP_URLS
from src.trial import trial_tail
from tests.test_server import build_with_upstream, make_settings

CONNECT = "https://hotels.flightpowers.test/connect"

#: Comparative / superlative claims a directory reviewer reads as promotion.
SUPERLATIVE = re.compile(
    r"no other|nothing else|only (travel|tool|server|one)|unlike (other|any)"
    r"|cannot do|can't do|better than|the first (travel|mcp)"
    r"|what this server can do|ad-free",
    re.IGNORECASE,
)


async def _tools_json(products: str) -> list[dict]:
    # The production default: compare_hotel_rates is off (MCP_HOTEL_COMPARE
    # unset), so this is the surface a directory reviewer connects to.
    mcp = build_with_upstream(
        lambda _r: httpx.Response(200, json=[]),
        products=products,
        hotel_compare_enabled=False,
    )
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return [t.model_dump(mode="json") for t in tools]


def _instructions(products: str, with_trial: bool) -> str:
    return build_instructions(
        make_settings(
            products=products,
            signup_url=DEFAULT_SIGNUP_URLS[products],
            # The production default (MCP_HOTEL_COMPARE unset).
            hotel_compare_enabled=False,
        ),
        trial_tail=trial_tail(10, CONNECT) if with_trial else "",
        connect_url=CONNECT,
    )


class TestNoToolOffersAirbnb:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    async def test_no_tool_schema_or_text_names_airbnb(self, products):
        for tool in await _tools_json(products):
            blob = json.dumps(tool).lower()
            assert "airbnb" not in blob, f"{tool['name']} names airbnb"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ["hotels", "both"])
    async def test_search_hotels_takes_no_providers(self, products):
        tools = {t["name"]: t for t in await _tools_json(products)}
        props = tools["search_hotels"]["inputSchema"]["properties"]
        assert "providers" not in props
        # The tool still answers from Booking.com, its only source here.
        assert "Booking.com" in tools["search_hotels"]["description"]

    @pytest.mark.parametrize("products", ["hotels", "both"])
    def test_instructions_name_no_second_source(self, products):
        for with_trial in (True, False):
            text = _instructions(products, with_trial).lower()
            assert "airbnb" not in text
            assert "`providers`" not in text


class TestNoSuperlatives:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    async def test_no_tool_text_claims_to_beat_anything(self, products):
        for tool in await _tools_json(products):
            blob = json.dumps(tool)
            hit = SUPERLATIVE.search(blob)
            assert hit is None, f"{tool['name']} carries {hit.group(0)!r}"

    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    def test_instructions_claim_nothing_comparative(self, products):
        for with_trial in (True, False):
            text = _instructions(products, with_trial)
            hit = SUPERLATIVE.search(text)
            assert hit is None, f"instructions carry {hit.group(0)!r}"


class TestInstructionsCarryNoKeyHowto:
    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    def test_no_listing_url_header_or_query_key(self, products):
        for with_trial in (True, False):
            text = _instructions(products, with_trial)
            assert "rapidapi.com" not in text
            assert "x-rapidapi-key" not in text
            assert "?rapidapi_key" not in text
            assert "rapidapi_key=" not in text

    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    def test_the_allowance_and_connect_stay(self, products):
        text = _instructions(products, with_trial=True)
        assert "10 free searches a day after sign-in with Google" in text
        assert f"with the caller's own RapidAPI key, connected at {CONNECT}, searches run on their RapidAPI plan" in text
        # Said once: the allowance sentence already names /connect.
        assert text.count(CONNECT) == 1

    @pytest.mark.parametrize("products", ["hotels", "both", "flights"])
    def test_without_the_allowance_connect_is_still_named(self, products):
        text = _instructions(products, with_trial=False)
        assert f"A RapidAPI key connected once at {CONNECT}" in text
