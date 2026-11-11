"""`trial.upgrade_hint`: the price, said at the moment of need, to keyless
allowance users only (2026-11-11).

Two moments: the day's allowance is 80% spent, or the allowance itself
sampled a wider question. Every other allowance result keeps the 2026-10-22
shape (counter + /connect, no price), and a caller with their own key --
paying or not -- never gets a `trial` block at all, so never this hint
(Matan 2026-11-11: no nudges for paying people).
"""

import json

import pytest

from src.fanout import plan_hotel_stays, plan_oneway
from src.server import _trial_sampling
from src.trial import TrialState, trial_note, utc_day
from tests.test_oauth import HEADER_KEY, SUB, Session, call_tool
from tests.test_trial import SEARCH_ARGS, _bearer, _build_trial

FLIGHTS = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
HOTELS = "https://rapidapi.com/mtnrabi/api/booking-live-api"
CONNECT = "https://flights.example/connect"

#: The keys `trial_note` carried before this change, on every result.
OLD_KEYS = {"used_today", "day_cap", "remaining_today", "key_connected", "user", "human", "note"}


def _state(used, cap=10):
    return TrialState("sub-1", "a@b.c", used, cap)


@pytest.mark.parametrize("used", [0, 1, 5, 7])
def test_below_80_percent_the_note_is_unchanged(used):
    note = trial_note(_state(used), CONNECT, FLIGHTS)
    assert set(note) == OLD_KEYS
    assert "$" not in json.dumps(note)


@pytest.mark.parametrize("used,left", [(8, 2), (9, 1), (10, 0)])
def test_from_80_percent_the_note_names_the_price(used, left):
    note = trial_note(_state(used), CONNECT, FLIGHTS)
    hint = note["upgrade_hint"]
    assert hint.startswith(f"{left} of 10 free searches left today.")
    assert "PRO is $10 a month for 2,500 searches" in hint
    assert f"{FLIGHTS}/pricing" in hint
    assert CONNECT in hint
    # The rest of the block is what it always was.
    assert set(note) - {"upgrade_hint"} == OLD_KEYS
    assert "$" not in note["note"] and "$" not in note["human"]


def test_hotels_quote_the_hotels_ladder():
    hint = trial_note(_state(9), CONNECT, HOTELS)["upgrade_hint"]
    assert "PRO is $10 a month for 2,000 searches" in hint
    assert f"{HOTELS}/pricing" in hint


def test_a_question_the_allowance_sampled_says_so():
    hint = trial_note(_state(3), CONNECT, FLIGHTS, requested=50, searched=7)["upgrade_hint"]
    assert hint.startswith("This question expanded to 50 searches; the free allowance ran 7")
    assert "PRO is $10 a month for 2,500 searches" in hint


def test_sampling_is_only_reported_when_the_allowance_did_it():
    wide = plan_oneway(
        from_airport="TLV",
        to_airport="ATH",
        departure_date_from="2026-12-01",
        departure_date_to="2026-12-31",
        cap=30,
    )
    assert _trial_sampling(wide.recap(5, cap_source="explicit")) == {}
    assert _trial_sampling(wide.recap(31, cap_source="trial")) == {}  # not truncated
    assert _trial_sampling(wide.recap(5, cap_source="trial")) == {"requested": 31, "searched": 5}


def test_a_trial_sampled_plan_is_not_told_it_was_billed():
    wide = plan_oneway(
        from_airport="TLV",
        to_airport="ATH",
        departure_date_from="2026-12-01",
        departure_date_to="2026-12-31",
        cap=30,
    ).recap(5, cap_source="trial")
    note = wide.coverage()["note"]
    assert "nothing was billed to you" in note
    assert "billed to your plan" not in note
    stays = plan_hotel_stays(
        checkin_date_from="2026-12-01", checkin_date_to="2026-12-20", nights=2, cap=30
    ).recap(4, cap_source="trial")
    assert "nothing was billed to you" in stays.coverage()["note"]
    # A keyed caller's own cap keeps the old wording.
    keyed = plan_oneway(
        from_airport="TLV",
        to_airport="ATH",
        departure_date_from="2026-12-01",
        departure_date_to="2026-12-31",
        cap=30,
    ).recap(5, cap_source="explicit")
    assert "billed to your plan" in keyed.coverage()["note"]


async def test_the_last_searches_of_the_day_carry_the_hint(monkeypatch):
    trial = _build_trial(monkeypatch, cap=10)
    async with Session(trial) as session:
        token = await _bearer(session.http)
        first = await call_tool(session.http, "/mcp", SEARCH_ARGS, {"authorization": f"Bearer {token}"})
        assert first["trial"]["used_today"] == 1
        assert "upgrade_hint" not in first["trial"]
        await trial.trial_store.spend(SUB, utc_day(), 7)
        late = await call_tool(session.http, "/mcp", SEARCH_ARGS, {"authorization": f"Bearer {token}"})
    assert late["trial"]["used_today"] == 9
    assert "PRO is $10 a month for 2,500 searches" in late["trial"]["upgrade_hint"]


async def test_a_caller_with_their_own_key_never_sees_it(monkeypatch):
    trial = _build_trial(monkeypatch, cap=10)
    async with Session(trial) as session:
        reply = await call_tool(session.http, "/mcp", SEARCH_ARGS, {"x-rapidapi-key": HEADER_KEY})
    assert "trial" not in reply
    assert "upgrade_hint" not in json.dumps(reply)
