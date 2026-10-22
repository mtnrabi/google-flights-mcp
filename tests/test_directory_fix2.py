"""Fix round 2 of the directory PR (critic 2026-10-22, findings 2 and 3).

Finding 2: `compare_hotel_rates` compared Booking with an Airbnb source the api
front answers `provider_unavailable`, so it is off on a deployment built from
the environment until a second source is live; and the privacy / support pages
say what the server and its backends actually keep.

Finding 3: the price is said only where a user hits a wall. `instructions` and
the note on every allowance result carry no price, no plan name and no pricing
link; the cap result, the quota refusals, the missing-key refusal, the 401 body
and /connect keep the price and the listing's pricing tab.
"""

from __future__ import annotations

import re

import httpx
import pytest
from fastmcp import Client

from src.legal import render_document, support_html
from src.quota_gate import plan_quota_refusal, trial_quota_refusal
from src.server import build_instructions
from src.settings import DEFAULT_SIGNUP_URLS, load_settings
from src.trial import TrialState, trial_exhausted_result, trial_note, trial_tail
from tests.test_server import build_with_upstream, make_settings

#: What a price or a plan pitch looks like in text a directory reviewer reads.
PRICE = re.compile(r"\$\s?\d|\bPRO\b|\bBASIC\b|/pricing\b|free tier|a month for")


def _no_price(text: str, where: str) -> None:
    hit = PRICE.search(text)
    assert hit is None, f"{where} carries {hit.group(0)!r}: {text[:300]}"


# ── finding 2: compare_hotel_rates is off until a second source is live ──


class TestCompareIsOffByDefault:
    def test_the_environment_default_is_off(self, monkeypatch):
        monkeypatch.delenv("MCP_HOTEL_COMPARE", raising=False)
        assert load_settings("hotels").hotel_compare_enabled is False

    def test_on_turns_it_back_on_without_code(self, monkeypatch):
        monkeypatch.setenv("MCP_HOTEL_COMPARE", "on")
        assert load_settings("hotels").hotel_compare_enabled is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ["hotels", "both"])
    async def test_off_means_not_in_tools_list(self, products):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products=products,
            hotel_compare_enabled=False,
        )
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert "compare_hotel_rates" not in names
        assert {"search_hotels", "find_hotel_by_name"} <= names

    @pytest.mark.asyncio
    async def test_on_keeps_it(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]),
            products="hotels",
            hotel_compare_enabled=True,
        )
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert "compare_hotel_rates" in names

    @pytest.mark.parametrize("products", ["hotels", "both"])
    def test_instructions_never_name_a_tool_that_is_not_served(self, products):
        off = build_instructions(
            make_settings(products=products, hotel_compare_enabled=False)
        )
        on = build_instructions(
            make_settings(products=products, hotel_compare_enabled=True)
        )
        assert "compare_hotel_rates" not in off
        assert "compare_hotel_rates" in on
        # `search_hotels` lost `providers` on 2026-10-22 (test_directory_fix3).
        assert "`providers` on `search_hotels`" not in off


class TestPolicyPagesSayWhatIsKept:
    @pytest.mark.parametrize("products", ["flights", "hotels", "both"])
    def test_the_allowance_table_is_disclosed(self, products):
        doc = render_document("privacy", products)
        assert "The daily sign-in allowance" in doc
        assert "One row per Google account per UTC day" in doc
        assert "does <strong>not</strong> delete them" in doc
        assert "only identity this service ever holds" not in doc
        assert "Effective date:</strong> 2026-10-22" in doc

    @pytest.mark.parametrize("products", ["flights", "hotels", "both"])
    def test_the_backend_logs_are_disclosed(self, products):
        doc = render_document("privacy", products)
        assert "keep" in doc and "short-lived logs" in doc
        assert "about one day" in doc
        assert "[paid_trial]" in doc

    def test_hotels_says_the_google_id_goes_to_the_hotels_backend(self):
        doc = render_document("privacy", "hotels")
        assert "sends your Google account identifier to that backend" in doc
        assert "There is no fan-out within a source" not in doc
        assert "one request per stay" in doc

    def test_flights_says_the_allowance_runs_on_our_account(self):
        doc = render_document("privacy", "flights")
        assert "that is FlightPowers&#x27; own account, not yours" in doc

    @pytest.mark.parametrize("products", ["flights", "hotels"])
    def test_support_no_longer_says_no_identity_is_stored(self, products):
        page = support_html(products)
        assert "stores no user identity" not in page
        assert "include the email address" in page


# ── finding 3: the price only where a user hits a wall ───────────────────

CONNECT = "https://flights.example.test/connect"


def _state(used: int = 1, cap: int = 10) -> TrialState:
    return TrialState("sub-1", "a@b.c", used, cap)


class TestNoPriceBeforeAWall:
    @pytest.mark.parametrize("products", ["flights", "hotels", "both"])
    def test_instructions_carry_no_price(self, products):
        text = build_instructions(
            make_settings(products=products, signup_url=DEFAULT_SIGNUP_URLS[products]),
            trial_tail=trial_tail(10, CONNECT),
            connect_url=CONNECT,
        )
        _no_price(text, f"{products} instructions")
        assert "10 free searches a day after sign-in" in text
        assert f"connect your own RapidAPI key at {CONNECT} for more" in text

    def test_the_note_on_every_result_is_the_counter_and_connect(self):
        note = trial_note(_state(), CONNECT, DEFAULT_SIGNUP_URLS["flights"])
        _no_price(note["note"], "trial_note")
        _no_price(note["human"], "trial_note human")
        assert CONNECT in note["note"]
        assert "rapidapi.com" not in note["note"]
        assert "1 of 10 free searches used today" in note["human"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("products", ["flights", "hotels"])
    async def test_tool_descriptions_carry_no_price(self, products):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products=products
        )
        async with Client(mcp) as client:
            tools = await client.list_tools()
        for tool in tools:
            _no_price(tool.description or "", f"{tool.name} description")


class TestTheWallsKeepThePrice:
    def test_the_cap_hit_names_pro_and_the_pricing_tab(self):
        signup = DEFAULT_SIGNUP_URLS["flights"]
        result = trial_exhausted_result(
            _state(used=10), CONNECT, signup, "Google Flights Live API"
        )
        assert "PRO is $10 a month for 2,500 searches" in result["message"]
        assert f"{signup}/pricing" in result["message"]

    def test_the_hotels_cap_hit_quotes_the_hotels_quota(self):
        signup = DEFAULT_SIGNUP_URLS["hotels"]
        result = trial_exhausted_result(
            _state(used=10), CONNECT, signup, "Booking Live API"
        )
        assert "PRO is $10 a month for 2,000 searches" in result["message"]

    def test_the_allowance_quota_refusal_names_pro(self):
        signup = DEFAULT_SIGNUP_URLS["flights"]
        refusal = trial_quota_refusal(
            requested=93,
            remaining_today=4,
            day_cap=10,
            signup_url=signup,
            connect_url=CONNECT,
        )
        assert "PRO is $10 a month" in refusal["message"]
        assert refusal["pricing_url"] == f"{signup}/pricing"

    def test_the_plan_quota_refusal_links_the_pricing_tab(self):
        signup = DEFAULT_SIGNUP_URLS["flights"]
        refusal = plan_quota_refusal(
            requested=93, remaining_month=4, spent_probing=1, signup_url=signup
        )
        assert f"{signup}/pricing" in refusal["message"]
