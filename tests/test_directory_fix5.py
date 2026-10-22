"""
Directory fix round 3 on PR #534 (2026-10-22, review r2).

1. Every month-wide promise ("the cap rises by itself", "a whole month is one
   call", "not thirty") is scoped to the caller's own key on a deployment with
   the free sign-in allowance: the instructions (flights and hotels), the
   one-way description, both hotel range descriptions and every
   `max_searches` parameter. A free user gets 10 a day and a bigger grid is
   refused before it runs. Deployments without the allowance keep their text.
2. "Never reuse an earlier result" no longer pushes a free user to spend a
   search on a question that needs no fresh price.
3. The size-refusal card says "searches" to a free user.
"""

import httpx
import pytest
from fastmcp import Client

from src.server import build_instructions
from tests.test_directory_fix4 import (  # noqa: F401 - fixtures
    _plain_tools,
    _trial_tools,
    flights,
    hotels,
)
from tests.test_server import build_with_upstream, make_settings

ALLOWANCE = "10 free searches a day."


# ── 1. month-wide advice is scoped to a key ──────────────────────────────


class TestInstructions:
    def test_flights_paragraph_is_scoped_on_the_allowance(self):
        text = build_instructions(
            make_settings(products="flights"), trial_tail=ALLOWANCE
        )
        assert "and, with the caller's own key, the cap RISES BY ITSELF" in text
        assert "On the free sign-in allowance it does not rise" in text
        assert "refused before anything runs" in text

    def test_hotels_paragraph_is_scoped_on_the_allowance(self):
        text = build_instructions(
            make_settings(products="hotels"), trial_tail=ALLOWANCE
        )
        assert (
            "With the caller's own key the cap rises by itself to cover a "
            "month-sized range." in text
        )
        assert "fewer check-in dates or stay lengths" in text

    @pytest.mark.parametrize("products", ["flights", "hotels", "both"])
    def test_without_the_allowance_nothing_changes(self, products):
        text = build_instructions(make_settings(products=products))
        assert "free sign-in" not in text
        assert "{allowance}" not in text and "{own_key}" not in text
        if products != "hotels":
            assert "per call, and the cap RISES BY ITSELF" in text


class TestToolText:
    async def test_oneway_carries_the_allowance_line(self, flights):
        desc = (await _trial_tools(flights))["search_oneway_flights"][
            "description"
        ]
        assert "not thirty. That holds with the caller's own key." in desc
        assert "a whole-month scan needs the caller's own key" in desc

    async def test_hotel_ranges_carry_the_allowance_line(self, hotels):
        tools = await _trial_tools(hotels)
        for name in ("search_hotels", "find_hotel_by_name"):
            desc = tools[name]["description"]
            assert "On the free sign-in allowance (10 searches a day" in desc, name
            assert "a wider range needs the caller's own key" in desc, name

    async def test_no_allowance_line_without_the_allowance(self):
        tools = await _plain_tools()
        assert "free sign-in" not in tools["search_oneway_flights"]["description"]
        hotels_mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(hotels_mcp) as client:
            for tool in await client.list_tools():
                assert "free sign-in" not in (tool.description or ""), tool.name

    async def test_every_max_searches_scopes_the_raise_to_a_key(self):
        tools = dict(await _plain_tools())
        hotels_mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(hotels_mcp) as client:
            for tool in await client.list_tools():
                tools[tool.name] = tool.model_dump(mode="json")
        checked = 0
        for name, tool in tools.items():
            prop = tool["inputSchema"]["properties"].get("max_searches")
            if prop is None:
                continue
            checked += 1
            assert "with the caller's own key" in prop["description"], name
        assert checked >= 4


# ── 2. staleness ─────────────────────────────────────────────────────────


class TestStaleness:
    async def test_no_tool_says_never_reuse(self):
        tools = await _plain_tools()
        hotels_mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products="hotels"
        )
        async with Client(hotels_mcp) as client:
            for tool in await client.list_tools():
                tools[tool.name] = tool.model_dump(mode="json")
        for name, tool in tools.items():
            desc = tool["description"] or ""
            assert "never reuse" not in desc.lower(), name
            if "stale within minutes" in desc:
                assert "a current price comes from a new search" in desc or (
                    "a current comparison comes from a new call" in desc
                ), name

    @pytest.mark.parametrize("products", ["flights", "hotels"])
    def test_instructions(self, products):
        text = build_instructions(make_settings(products=products))
        assert "Never reuse" not in text
        assert "a current price comes from a new search" in text
        assert "each price belongs with the time it was fetched" in text


# ── 3. the refusal card says searches to a free user ─────────────────────


class TestRefusalCard:
    def test_rendered(self):
        from tests.test_widget_render import (
            REFUSAL,
            REFUSAL_PROBE,
            _chrome_or_skip,
            _inline,
            _run_page,
        )

        chrome = _chrome_or_skip()
        free = dict(
            REFUSAL,
            combos_requested=31,
            combos_allowed_now=10,
            trial={"day_cap": 10, "remaining_today": 10, "key_connected": False},
        )
        got = _run_page(
            chrome, REFUSAL_PROBE.replace("__PAYLOAD__", _inline(free)), width=760
        )
        assert got["quota"] == "31 searches needed · 10 left"
        keyed = _run_page(
            chrome, REFUSAL_PROBE.replace("__PAYLOAD__", _inline(REFUSAL)), width=760
        )
        assert keyed["quota"] == "93 requests needed · 9 left"
