"""
The keyless allowance: a signed-in caller with no RapidAPI key of their own.

Driven through the real transport (`tests/test_oauth.py`'s harness), not by
calling the resolver, for the same reason day 2 and day 3 are: the identity
this whole feature spends money on arrives as a header the OAuth gate injects
after validating a token, and a unit test of `_resolve` would prove the
function rather than the endpoint.

The properties that would cost something if they broke:

* **A key always beats the allowance.** A header key, and a stored key, are
  both the caller's own money and both must win -- a paying integration that
  quietly started running on ours would be a bill nobody is watching.
* **The cap is per account and per UTC day.** Not global (one caller would
  spend everybody's), not per tool call (a date range is 30 backend searches).
* **Over the cap is a RESULT, not an error.** A model handed an exception
  retries it, and a retry cannot succeed until tomorrow.
* **Our key never leaves without attribution.** Rule 11: every backend caller
  is attributable and counted.
"""

import asyncio
import datetime as dt
import json

import httpx
import pytest

import src.keystore as keystore_module
import src.oauth as oauth_module
import src.server as server_module
from src.entrypoint import build_entrypoint
from src.keystore import MemoryKeyStore
from src.oauthstore import MemoryOAuthStore
from src.settings import load_settings
from src.trial import (
    KEYED_SOURCE,
    REASON_SPENT,
    REASON_UNAVAILABLE,
    TRIAL_EXHAUSTED,
    TrialState,
    body_tags,
    keyed_body_tags,
    trial_exhausted_result,
    trial_note,
    utc_day,
)
from src.trialstore import (
    _RESERVE,
    MemoryTrialStore,
    NullTrialStore,
    TrialStoreUnavailable,
    build_trial_store,
)
from tests.test_oauth import (
    EMAIL,
    GOOGLE_CLIENT_ID,
    HEADER_KEY,
    MASTER,
    MASTER_B64,
    ORIGIN,
    SUB,
    USER_KEY,
    Session,
    Upstream,
    _granted_access_token,
    call_tool,
)

TRIAL_KEY = "trial-key-" + "t" * 40
SEARCH_ARGS = {
    "departure_date": "2026-09-20",
    "from_airport": "TLV",
    "to_airport": "ATH",
}

#: One stay, on the product whose backend accepts `_fp_user`.
HOTEL_ARGS = {
    "destination": "Rome",
    "checkin_date": "2026-10-10",
    "checkout_date": "2026-10-12",
}


class TaggedUpstream(Upstream):
    """The harness's fake RapidAPI, plus a record of every request BODY.

    The body is where attribution rides on this path -- the Hub strips custom
    headers -- so a test that only watched headers would pass while the
    traffic went out anonymous.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not url.startswith("https://oauth2.googleapis.com/token"):
            try:
                self.bodies.append(json.loads(request.content or b"{}"))
            except ValueError:
                pass
        return super().__call__(request)


class Deployment:
    def __init__(self, app, key_store, trial_store, upstream) -> None:
        self.app = app
        self.key_store = key_store
        self.trial_store = trial_store
        self.upstream = upstream


def _build_trial(
    monkeypatch,
    *,
    cap: int = 3,
    trial_key: str = TRIAL_KEY,
    products: str = "flights",
):
    """One built process with OAuth on and the allowance configured.

    Deliberately a local copy of `tests/test_oauth._build` rather than a
    parameter added to it: this file needs two extra env vars and a store
    nothing else knows about, and threading them through the shared helper
    would change the shape of forty tests that do not care.
    """
    for name in (
        "MCP_PRODUCTS_BY_HOST",
        "MCP_PUBLIC_URL_FLIGHTS",
        "MCP_PUBLIC_URL_HOTELS",
        "SIGNUP_URL",
        "RAPIDAPI_KEY",
        "MCP_OAUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", products)
    monkeypatch.setenv("MCP_PRODUCTS_BY_HOST", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{ORIGIN}/mcp")
    monkeypatch.setenv("RAPIDAPI_BASE_URL", "https://upstream.test")
    monkeypatch.setenv("RAPIDAPI_HOST", "upstream.test")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
    monkeypatch.setenv("MCP_KEY_MASTER", MASTER_B64)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused-in-tests/db")
    monkeypatch.setenv("PAID_TRIAL_DAY_CAP", str(cap))
    if trial_key:
        monkeypatch.setenv("PAID_TRIAL_RAPIDAPI_KEY", trial_key)
    else:
        monkeypatch.delenv("PAID_TRIAL_RAPIDAPI_KEY", raising=False)

    key_store = MemoryKeyStore(MASTER)
    trial_store = MemoryTrialStore()
    monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: key_store)
    monkeypatch.setattr(
        oauth_module, "build_oauth_store", lambda *a, **k: MemoryOAuthStore()
    )
    monkeypatch.setattr(server_module, "build_trial_store", lambda *a, **k: trial_store)

    upstream = TaggedUpstream()
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    return Deployment(build_entrypoint().app, key_store, trial_store, upstream)


@pytest.fixture
def trial(monkeypatch):
    return _build_trial(monkeypatch)


@pytest.fixture
def trial_hotels(monkeypatch):
    """The same allowance, on the hotels product."""
    return _build_trial(monkeypatch, products="hotels")


@pytest.fixture
def no_trial(monkeypatch):
    """OAuth on, allowance off -- the state of production before this PR."""
    return _build_trial(monkeypatch, cap=0, trial_key="")


async def _bearer(http) -> str:
    return await _granted_access_token(http)


# ── 1. a signed-in caller with no key gets a real search ─────────────────


class TestTheAllowanceServesASignedInCaller:
    async def test_a_search_runs_on_our_key_and_says_so(self, trial):
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert "needs_api_key" not in reply
        assert reply["result_count"] == 1
        # The key that went upstream is ours, and the reply says whose it is.
        assert trial.upstream.keys_seen == [TRIAL_KEY]
        assert reply["trial"]["used_today"] == 1
        assert reply["trial"]["day_cap"] == 3
        assert reply["trial"]["remaining_today"] == 2
        assert reply["trial"]["key_connected"] is False
        assert f"{ORIGIN}/connect" in reply["trial"]["note"]

    async def test_the_allowance_is_off_without_a_cap_and_a_key(self, no_trial):
        """The behaviour production has today: signed in, no key, no search."""
        async with Session(no_trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["needs_api_key"] is True
        assert "trial" not in reply
        assert no_trial.upstream.keys_seen == []

    async def test_health_states_the_allowance(self, trial, no_trial):
        async with Session(trial) as session:
            body = (await session.http.get("/health")).json()
        assert body["trial_enabled"] is True
        assert body["trial_day_cap"] == 3
        async with Session(no_trial) as session:
            body = (await session.http.get("/health")).json()
        assert body["trial_enabled"] is False
        assert body["trial_day_cap"] == 0


# ── 2. what is counted is a date x destination combination ───────────────


class TestWhatCounts:
    async def test_a_range_spends_one_per_combination(self, trial):
        """One tool call, three dates, three backend searches -- and three off
        the allowance. Counting tool calls would leave the shape that costs
        real money completely uncapped."""
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                {
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                    "departure_date_from": "2026-09-20",
                    "departure_date_to": "2026-09-22",
                },
                {"authorization": f"Bearer {token}"},
            )
        assert len(trial.upstream.keys_seen) == 3
        assert reply["trial"]["used_today"] == 3
        assert await trial.trial_store.usage(SUB, utc_day()) == 3

    async def test_the_last_call_of_the_day_cannot_overshoot(self, trial):
        """A caller at 2 of 3 asking for ten dates gets a refusal, not a
        one-date answer dressed up as ten.

        It used to get one search -- the allowance's remainder -- and a
        `search_coverage` saying so. That was safe for the counter and wrong
        for the user: "cheapest over these ten days" answered from one day
        reads as an answer. Nothing is searched and nothing is spent; the
        reply carries both numbers and the two ways forward.
        """
        await trial.trial_store.spend(SUB, utc_day(), 2)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                {
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                    "departure_date_from": "2026-09-20",
                    "departure_date_to": "2026-09-29",
                },
                {"authorization": f"Bearer {token}"},
            )
        assert trial.upstream.keys_seen == []
        assert await trial.trial_store.usage(SUB, utc_day()) == 2
        assert reply["search_status"] == "quota_exceeded"
        assert reply["combos_requested"] == 10
        assert reply["combos_allowed_now"] == 1
        assert reply["remaining_today"] == 1
        assert "needs 10 requests" in reply["message"]
        assert "1 left today" in reply["message"]

    async def test_a_request_the_allowance_covers_still_runs(self, trial):
        """The gate is "cannot pay", not "asked for a lot"."""
        await trial.trial_store.spend(SUB, utc_day(), 2)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                {
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                    "departure_date": "2026-09-20",
                },
                {"authorization": f"Bearer {token}"},
            )
        assert len(trial.upstream.keys_seen) == 1
        assert reply["trial"]["used_today"] == 3
        assert reply["trial"]["remaining_today"] == 0


# ── 3. over the cap ──────────────────────────────────────────────────────


class TestOverTheCap:
    async def test_it_is_a_result_not_an_error(self, trial):
        await trial.trial_store.spend(SUB, utc_day(), 3)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["search_status"] == TRIAL_EXHAUSTED
        assert reply["retry"] is False
        assert reply["results"] == []
        assert reply["trial"]["exhausted"] is True
        # Nothing was spent on a refusal.
        assert trial.upstream.keys_seen == []
        assert await trial.trial_store.usage(SUB, utc_day()) == 3

    async def test_it_points_at_connect_and_at_the_listing(self, trial):
        await trial.trial_store.spend(SUB, utc_day(), 3)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert f"{ORIGIN}/connect" in reply["message"]
        assert "google-flights-live-api" in reply["message"]
        assert reply["how_to_get_a_key"]["connect_url"] == f"{ORIGIN}/connect"

    async def test_the_transport_still_answers_200(self, trial):
        """Never a 401 and never a 429 on a tool call: an OAuth-capable client
        turns a 401 into a sign-in button, and this caller is already signed
        in. `call_tool` asserts the status itself."""
        await trial.trial_store.spend(SUB, utc_day(), 3)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["search_status"] == TRIAL_EXHAUSTED


# ── 4. a key always beats the allowance ──────────────────────────────────


class TestAKeyAlwaysWins:
    async def test_a_stored_key_is_used_instead_of_ours(self, trial):
        await trial.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert trial.upstream.keys_seen == [USER_KEY]
        assert "trial" not in reply
        assert await trial.trial_store.usage(SUB, utc_day()) == 0

    async def test_a_header_key_beats_a_bearer_token(self, trial):
        """Rule 6's order, with the allowance added at the end of it: every
        request-supplied channel still wins."""
        await trial.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {
                    "authorization": f"Bearer {token}",
                    "x-rapidapi-key": HEADER_KEY,
                },
            )
        assert trial.upstream.keys_seen == [HEADER_KEY]
        assert "trial" not in reply
        assert await trial.trial_store.usage(SUB, utc_day()) == 0

    async def test_a_header_key_wins_even_with_no_stored_key(self, trial):
        async with Session(trial) as session:
            token = await _bearer(session.http)
            await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {
                    "authorization": f"Bearer {token}",
                    "x-rapidapi-key": HEADER_KEY,
                },
            )
        assert trial.upstream.keys_seen == [HEADER_KEY]
        assert await trial.trial_store.usage(SUB, utc_day()) == 0

    async def test_an_anonymous_caller_is_not_served_from_it(self, trial):
        """No bearer, no key: the allowance is for a signed-in account, and a
        request with no identity at all must not reach it. `/mcp` answers
        those with the OAuth challenge, so nothing is spent."""
        async with Session(trial) as session:
            response = await session.http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_oneway_flights",
                        "arguments": SEARCH_ARGS,
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
        assert response.status_code == 401
        assert trial.upstream.keys_seen == []


# ── 5. attribution (rule 11) ─────────────────────────────────────────────


class TestAttribution:
    async def test_every_trial_request_body_names_the_source_and_tool(self, trial):
        async with Session(trial) as session:
            token = await _bearer(session.http)
            await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        searches = [b for b in trial.upstream.bodies if "from_airport" in b]
        assert searches, "no search body was captured"
        for body in searches:
            assert body["_fp_source"] == "paid-trial"
            assert body["_fp_tool"] == "search_oneway_flights"

    async def test_a_keyed_request_is_never_tagged_as_the_allowance(self, trial):
        """The tags exist to separate OUR spend from the caller's, and that is
        the half that must never blur: a paying customer's search attributed to
        the trial surface is a number we would then plan on.

        It is still tagged -- as `paid-mcp`, its own surface. Sending nothing
        left every keyed search this server makes indistinguishable from a
        customer curling the Hub (`source=rapidapi`), so the one value the
        server ever put on a `[lane]` line was `paid-trial`, and the paid MCP
        server's own traffic could not be counted at all."""
        async with Session(trial) as session:
            token = await _bearer(session.http)
            await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {
                    "authorization": f"Bearer {token}",
                    "x-rapidapi-key": HEADER_KEY,
                },
            )
        searches = [b for b in trial.upstream.bodies if "from_airport" in b]
        assert searches
        for body in searches:
            assert body["_fp_source"] == KEYED_SOURCE
            assert body["_fp_source"] != "paid-trial"
            assert body["_fp_tool"] == "search_oneway_flights"
            # Never on a keyed call, on any product: the Hub has already named
            # the owner of the key being billed, and `_fp_user` outranks it.
            assert "_fp_user" not in body

    async def test_a_keyed_hotels_request_is_tagged_without_a_user(self, trial_hotels):
        """The product that *does* take `_fp_user` is the one where getting
        this wrong would be invisible: a paying subscriber's `user=` would
        quietly become a Google subject id."""
        async with Session(trial_hotels) as session:
            token = await _bearer(session.http)
            await call_tool(
                session.http,
                "/mcp",
                HOTEL_ARGS,
                {
                    "authorization": f"Bearer {token}",
                    "x-rapidapi-key": HEADER_KEY,
                },
                tool="search_hotels",
            )
        stays = [b for b in trial_hotels.upstream.bodies if "destination" in b]
        assert stays
        for body in stays:
            assert body["_fp_source"] == KEYED_SOURCE
            assert body["_fp_tool"] == "search_hotels"
            assert "_fp_user" not in body

    def test_the_keyed_tags_are_the_same_fields_the_allowance_uses(self):
        """One vocabulary. A second spelling of `_fp_source` would be a second
        thing for the rollup to know about, and the day it did not know the
        traffic would vanish rather than be mislabelled."""
        assert keyed_body_tags("flights", "search_oneway_flights") == {
            "_fp_source": "paid-mcp",
            "_fp_tool": "search_oneway_flights",
        }
        assert keyed_body_tags("hotels", "search_hotels") == {
            "_fp_source": "paid-mcp",
            "_fp_tool": "search_hotels",
        }

    def test_the_flights_body_never_carries_fp_user(self):
        """`backend/src/api_lambda.py` strips `_fp_source` and `_fp_tool` and
        nothing else, and the flights request models are `extra="forbid"`, so
        an `_fp_user` in a flights body is a 422 -- and a validation reject
        prints nothing in CloudWatch, so it would fail silently."""
        assert body_tags("flights", "search_oneway_flights", SUB) == {
            "_fp_source": "paid-trial",
            "_fp_tool": "search_oneway_flights",
        }

    def test_the_hotels_body_does(self):
        """The hotels functions read X-FP-User -> _fp_user -> x-rapidapi-user,
        live on both since 2026-09-13, so `user=` on their log line can name
        the account a trial search belongs to."""
        assert body_tags("hotels", "search_hotels", SUB)["_fp_user"] == SUB


# ── 6. the counter ───────────────────────────────────────────────────────


class TestTheCounter:
    async def test_two_accounts_do_not_share_it(self, trial):
        """A global counter would let the first caller of the day spend the
        allowance every other caller was promised."""
        store = trial.trial_store
        await store.spend("sub-other", utc_day(), 3)
        assert await store.usage(SUB, utc_day()) == 0
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["result_count"] == 1
        assert reply["trial"]["used_today"] == 1
        assert await store.usage("sub-other", utc_day()) == 3

    async def test_it_rolls_over_at_utc_midnight(self, trial):
        store = trial.trial_store
        yesterday = utc_day() - dt.timedelta(days=1)
        await store.spend(SUB, yesterday, 3)
        assert await store.usage(SUB, yesterday) == 3
        assert await store.usage(SUB, utc_day()) == 0
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["result_count"] == 1
        assert reply["trial"]["used_today"] == 1

    def test_the_day_is_utc_not_local(self):
        naive = dt.datetime(2026, 9, 16, 23, 30, tzinfo=dt.timezone.utc)
        assert utc_day(naive) == dt.date(2026, 9, 16)
        assert utc_day(
            naive.astimezone(dt.timezone(dt.timedelta(hours=-8)))
        ) == dt.date(2026, 9, 16)

    async def test_a_spend_returns_the_stored_total(self):
        store = MemoryTrialStore()
        assert await store.spend("s", utc_day(), 2) == 2
        assert await store.spend("s", utc_day(), 3) == 5

    async def test_an_unconfigured_store_raises_rather_than_answering_zero(self):
        """Zero would read as "nothing spent yet", which is the one answer that
        must never be guessed: it hands every caller an uncapped run."""
        store = NullTrialStore()
        assert store.available is False
        with pytest.raises(TrialStoreUnavailable):
            await store.usage("s", utc_day())
        with pytest.raises(TrialStoreUnavailable):
            await store.spend("s", utc_day(), 1)

    def test_no_database_means_no_store(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert isinstance(build_trial_store(), NullTrialStore)
        assert build_trial_store("postgres://x/y").available is True


# ── 7. failures that are ours, not the caller's ──────────────────────────


class TestOurFailuresAreNotBlamedOnTheCaller:
    async def test_a_store_that_will_not_answer_asks_for_a_key(self, trial, monkeypatch):
        async def boom(*args, **kwargs):
            raise TrialStoreUnavailable("down")

        monkeypatch.setattr(trial.trial_store, "usage", boom)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        # Not "your allowance is spent" -- they have spent nothing.
        assert reply["needs_api_key"] is True
        assert reply.get("search_status") != TRIAL_EXHAUSTED
        assert trial.upstream.keys_seen == []

    async def test_our_key_being_refused_is_not_their_problem(self, trial):
        def refuse(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith("https://oauth2.googleapis.com/token"):
                return trial.upstream(request)
            if "google-flights-live-api.p.rapidapi.com" in url:
                return httpx.Response(200, json={"detail": "no dates"})
            return httpx.Response(403, json={"message": "not subscribed"})

        async with Session(trial) as session:
            token = await _bearer(session.http)
            server_module._shared_client = httpx.AsyncClient(
                transport=httpx.MockTransport(refuse)
            )
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
        assert reply["needs_api_key"] is True
        message = reply["message"]
        assert "problem on our side" in message
        assert "nothing was billed to you" in message
        # It must never read as "your key was rejected".
        assert "your key" not in message.lower()


# ── 8. the message objects, on their own ─────────────────────────────────


class TestTheCopy:
    def _state(self, used=1, cap=10):
        return TrialState(
            user_sub=SUB, email=EMAIL, used_today=used, day_cap=cap
        )

    def test_the_note_names_the_numbers_and_the_way_out(self):
        note = trial_note(self._state(), "https://mcp.test/connect", "https://rapid.test")
        assert note["used_today"] == 1
        assert note["day_cap"] == 10
        assert note["remaining_today"] == 9
        assert "1 of 10 free searches" in note["human"]
        assert "https://mcp.test/connect" in note["note"]
        assert "https://rapid.test" in note["note"]

    def test_the_refusal_says_retrying_will_not_help(self):
        reply = trial_exhausted_result(
            self._state(used=10),
            "https://mcp.test/connect",
            "https://rapid.test",
            "Google Flights Live API",
        )
        assert reply["retry"] is False
        assert "Retrying will not help" in reply["message"]
        assert "00:00 UTC" in reply["message"]
        assert reply["result_count"] == 0

    def test_no_message_contains_an_em_dash(self):
        """Every string here is read aloud to a human by an assistant."""
        note = trial_note(self._state(), "https://c.test", "https://r.test")
        reply = trial_exhausted_result(
            self._state(used=10), "https://c.test", "https://r.test", "API"
        )
        assert "—" not in json.dumps([note, reply])

    def test_remaining_never_goes_negative(self):
        assert TrialState(SUB, EMAIL, 99, 10).remaining == 0
        assert TrialState(SUB, EMAIL, 99, 10).exhausted is True
        assert TrialState(SUB, EMAIL, 0, 0).exhausted is True


# ── 9. the /connect page ─────────────────────────────────────────────────


class TestTheConnectPage:
    async def test_it_shows_the_allowance_to_a_signed_in_visitor(self, trial):
        from tests.test_oauth import sign_in

        await trial.trial_store.spend(SUB, utc_day(), 1)
        async with Session(trial) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert page.status_code == 200
        assert "Free allowance: 1 of 3 searches used today" in page.text
        assert "2 left" in page.text

    async def test_a_connected_key_is_told_the_cap_no_longer_applies(self, trial):
        from tests.test_oauth import sign_in

        await trial.key_store.put(SUB, EMAIL, USER_KEY)
        async with Session(trial) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert "no longer applies" in page.text

    async def test_a_deployment_without_the_allowance_says_nothing(self, no_trial):
        from tests.test_oauth import sign_in

        async with Session(no_trial) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert "Free allowance" not in page.text


# ── 10. the descriptions a client reads before it calls anything ─────────


class TestWhatTheToolsSay:
    async def test_the_allowance_is_in_the_instructions_and_descriptions(self, trial):
        async with Session(trial) as session:
            token = await _bearer(session.http)
            listed = await session.http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "authorization": f"Bearer {token}",
                },
            )
        assert listed.status_code == 200
        assert "first 3 searches each day are free" in listed.text

    async def test_a_deployment_without_it_promises_nothing(self, no_trial):
        async with Session(no_trial) as session:
            listed = await session.http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
        assert "searches each day are free" not in listed.text


# ── 11. the landing page a reviewer opens first ──────────────────────────


class TestTheLandingPage:
    async def test_it_says_there_is_nothing_to_paste(self, trial):
        async with Session(trial) as session:
            page = await session.http.get("/")
        assert page.status_code == 200
        assert "first 3 searches each UTC" in page.text
        assert "/connect" in page.text

    async def test_a_deployment_without_it_promises_nothing(self, no_trial):
        async with Session(no_trial) as session:
            page = await session.http.get("/")
        assert "searches each UTC" not in page.text


# ── 12. two calls at once must not both spend the allowance ──────────────


class TestConcurrency:
    """The reason the allowance is RESERVED before the searches run.

    Counting afterwards is not a cap, it is a report: two tool calls from one
    account in flight together both read "nothing spent yet", both run a full
    fan-out, and the day ends at twice the cap -- on our key, and invisibly,
    because each call on its own looks correct.
    """

    async def test_two_racing_tool_calls_cannot_exceed_the_cap(
        self, trial, monkeypatch
    ):
        """Two full-width searches, launched together, on one account, cap 3.

        The counter read is deliberately slowed so BOTH calls get past it
        holding "nothing spent yet" -- which is not a contrivance, it is the
        ordinary case on Vercel, where the two calls are two processes and
        neither can see the other's read. It is also the exact interleaving
        that made the old count-afterwards version spend 6 of a cap of 3.

        Exactly one gets to search. The other is refused, and the number of
        requests that reach the upstream is the cap, not twice it.
        """
        read_usage = trial.trial_store.usage
        both_have_read = asyncio.Barrier(2)
        pending = [2]

        async def slow_usage(*args, **kwargs):
            # A barrier, not a sleep: a sleep only makes the overlap likely,
            # and a race test that is merely likely to reproduce is a test
            # that will one day pass on a broken build. This holds BOTH calls
            # until both have read the counter, which is precisely what two
            # Vercel instances do, and then lets them both go on to reserve.
            #
            # It disarms after those two so the assertions below -- which read
            # the counter too -- do not wait for a third party that is never
            # coming.
            value = await read_usage(*args, **kwargs)
            if pending[0] > 0:
                pending[0] -= 1
                await asyncio.wait_for(both_have_read.wait(), 5)
            return value

        monkeypatch.setattr(trial.trial_store, "usage", slow_usage)
        async with Session(trial) as session:
            token = await _bearer(session.http)
            wide = {
                "from_airport": "TLV",
                "to_airport": "ATH",
                "departure_date_from": "2026-09-20",
                # Exactly the cap: wide enough that two of these together
                # would double it, narrow enough that neither is refused for
                # being unaffordable -- this test is about the RESERVATION
                # race, not about the size gate.
                "departure_date_to": "2026-09-22",
            }
            first, second = await asyncio.gather(
                call_tool(
                    session.http, "/mcp", wide, {"authorization": f"Bearer {token}"}
                ),
                call_tool(
                    session.http, "/mcp", wide, {"authorization": f"Bearer {token}"}
                ),
            )
        assert len(trial.upstream.keys_seen) == 3
        assert await trial.trial_store.usage(SUB, utc_day()) == 3
        statuses = {r.get("search_status") for r in (first, second)}
        assert TRIAL_EXHAUSTED in statuses

    async def test_the_loser_is_told_the_truth_not_a_wrong_number(self, trial):
        """A lost race is not "your allowance is spent" -- it may not be. The
        refusal says the allowance could not cover THIS search, and carries a
        `reason` a client can branch on."""
        state = TrialState(user_sub=SUB, email=EMAIL, used_today=3, day_cap=3)
        reply = trial_exhausted_result(
            state,
            "https://mcp.test/connect",
            "https://rapid.test",
            "API",
            reason=REASON_UNAVAILABLE,
        )
        assert reply["trial"]["reason"] == REASON_UNAVAILABLE
        assert "could not cover it right now" in reply["message"]
        assert "Retrying will not help" not in reply["message"]
        spent = trial_exhausted_result(
            state, "https://mcp.test/connect", "https://rapid.test", "API"
        )
        assert spent["trial"]["reason"] == REASON_SPENT
        assert "is spent" in spent["message"]

    async def test_racing_reservations_on_the_store_produce_one_winner(self):
        """The store-level property the server rests on: N coroutines asking
        for the whole cap at once, and the counter still lands on the cap."""
        store = MemoryTrialStore()
        results = await asyncio.gather(
            *(store.reserve(SUB, utc_day(), 5, 5) for _ in range(8))
        )
        assert sum(1 for r in results if r is not None) == 1
        assert await store.usage(SUB, utc_day()) == 5

    async def test_a_reservation_bigger_than_the_whole_cap_is_refused(self):
        """The INSERT branch of the SQL is not covered by its WHERE, so this
        one is checked in Python. A first-of-the-day reservation for more than
        the cap must not be the way through it."""
        store = MemoryTrialStore()
        assert await store.reserve(SUB, utc_day(), 11, 10) is None
        assert await store.usage(SUB, utc_day()) == 0

    def test_the_sql_checks_the_cap_in_one_statement(self):
        """A read, then a write, is the bug this replaced -- and it is the
        shape a later edit would most plausibly reintroduce. So the statement
        is asserted, not just its behaviour:

        * ONE statement (one INSERT, no separate SELECT);
        * the new total computed FROM the stored column, not from a value this
          process read a moment ago;
        * the cap in the WHERE, so a breach writes nothing;
        * RETURNING, so the winner learns the total without reading again.
        """
        sql = " ".join(_RESERVE.split())
        assert sql.count("INSERT") == 1
        assert "SELECT" not in sql.upper()
        assert "paid_trial_usage.searches + EXCLUDED.searches" in sql
        assert "WHERE paid_trial_usage.searches + EXCLUDED.searches <= $6" in sql
        assert sql.rstrip().endswith("RETURNING searches")


# ── 13. what was reserved and not spent comes back ───────────────────────


class TestTheRefund:
    async def test_combinations_that_never_ran_are_given_back(self, trial):
        """The reservation is the whole plan; the settle hands back whatever
        the fan-out did not actually send. Here every request fails in
        transport, so the plan reserved 2 and spent 2 -- the shape to prove is
        the store-level one below, which is what the settle calls."""
        store = MemoryTrialStore()
        assert await store.reserve(SUB, utc_day(), 10, 10) == 10
        assert await store.refund(SUB, utc_day(), 4) == 6
        assert await store.usage(SUB, utc_day()) == 6
        # The refunded room is usable again the same day.
        assert await store.reserve(SUB, utc_day(), 4, 10) == 10

    async def test_a_refund_cannot_push_the_counter_negative(self):
        """A settle that somehow ran twice must not hand out MORE than the
        cap. GREATEST(0, ...) in SQL, max(0, ...) here."""
        store = MemoryTrialStore()
        await store.reserve(SUB, utc_day(), 2, 10)
        await store.refund(SUB, utc_day(), 5)
        await store.refund(SUB, utc_day(), 5)
        assert await store.usage(SUB, utc_day()) == 0

    async def test_a_truncated_fanout_does_not_charge_for_what_it_dropped(
        self, trial
    ):
        """`max_searches` lowers the plan, so the reservation is the plan's
        size, not the request's. A caller who asks for ten dates with
        max_searches 2 is charged two."""
        async with Session(trial) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                {
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                    "departure_date_from": "2026-09-20",
                    "departure_date_to": "2026-09-29",
                    "max_searches": 2,
                },
                {"authorization": f"Bearer {token}"},
            )
        assert len(trial.upstream.keys_seen) == 2
        assert reply["trial"]["used_today"] == 2
        assert await trial.trial_store.usage(SUB, utc_day()) == 2


# ── 14. the allowance never turns itself on ──────────────────────────────


class TestItIsOffUnlessBothSwitchesAreSet:
    """This is the one setting on this server that spends OUR money, so it
    must be impossible to arrive at by accident -- by a default, by a preview
    inheriting production env, or by a variable that was set years ago for a
    different reason."""

    def test_the_default_cap_is_zero(self, monkeypatch):
        for name in ("PAID_TRIAL_DAY_CAP", "PAID_TRIAL_RAPIDAPI_KEY", "RAPIDAPI_KEY"):
            monkeypatch.delenv(name, raising=False)
        settings = load_settings("flights")
        assert settings.trial_day_cap == 0
        assert settings.trial_enabled is False

    def test_a_cap_without_a_key_is_off(self, monkeypatch):
        monkeypatch.setenv("PAID_TRIAL_DAY_CAP", "10")
        monkeypatch.delenv("PAID_TRIAL_RAPIDAPI_KEY", raising=False)
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        assert load_settings("flights").trial_enabled is False

    def test_a_key_without_a_cap_is_off(self, monkeypatch):
        monkeypatch.delenv("PAID_TRIAL_DAY_CAP", raising=False)
        monkeypatch.setenv("PAID_TRIAL_RAPIDAPI_KEY", TRIAL_KEY)
        assert load_settings("flights").trial_enabled is False

    def test_rapidapi_key_does_not_fund_the_allowance(self, monkeypatch):
        """`RAPIDAPI_KEY` means something else and something blunter: serve
        every keyless caller on the deployment owner's plan. Inheriting it
        here would turn one dangerous switch into two."""
        monkeypatch.setenv("PAID_TRIAL_DAY_CAP", "10")
        monkeypatch.delenv("PAID_TRIAL_RAPIDAPI_KEY", raising=False)
        monkeypatch.setenv("RAPIDAPI_KEY", "env-key-" + "z" * 40)
        settings = load_settings("flights")
        assert settings.trial_rapidapi_key == ""
        assert settings.trial_enabled is False

    def test_a_negative_cap_clamps_to_off_not_to_open(self, monkeypatch):
        monkeypatch.setenv("PAID_TRIAL_DAY_CAP", "-5")
        monkeypatch.setenv("PAID_TRIAL_RAPIDAPI_KEY", TRIAL_KEY)
        assert load_settings("flights").trial_day_cap == 0
        assert load_settings("flights").trial_enabled is False

    async def test_a_cap_with_no_key_serves_the_old_reply(self, monkeypatch):
        deployment = _build_trial(monkeypatch, cap=10, trial_key="")
        async with Session(deployment) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                SEARCH_ARGS,
                {"authorization": f"Bearer {token}"},
            )
            health = (await session.http.get("/health")).json()
        assert reply["needs_api_key"] is True
        assert health["trial_enabled"] is False
        assert health["trial_day_cap"] == 0
        assert deployment.upstream.keys_seen == []


# ── 15. comparing sources is not part of the allowance ───────────────────


class TestTheMultiSourcePathsAskForTheirOwnKey:
    """`_provider_keys` withdraws the allowance key before a cross-source
    call: those paths reach sources that are not all on the RapidAPI edge
    (Airbnb goes through the api front), so serving them from the allowance
    would bill us for the product we sell. Refused with a sentence, never
    quietly skipped."""

    COMPARE_ARGS = {
        "destination": "Rome",
        "checkin_date": "2026-10-10",
        "checkout_date": "2026-10-12",
    }

    async def test_compare_hotel_rates_is_refused(self, trial_hotels):
        async with Session(trial_hotels) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                self.COMPARE_ARGS,
                {"authorization": f"Bearer {token}"},
                tool="compare_hotel_rates",
            )
        assert reply["needs_api_key"] is True
        assert "free signed-in allowance covers single-source searches only" in (
            reply["message"]
        )
        assert f"{ORIGIN}/connect" in reply["message"]
        # Nothing was billed to us, and nothing was billed to them.
        assert trial_hotels.upstream.keys_seen == []
        assert await trial_hotels.trial_store.usage(SUB, utc_day()) == 0

    async def test_multi_provider_search_hotels_is_refused(self, trial_hotels):
        async with Session(trial_hotels) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                {**self.COMPARE_ARGS, "providers": ["booking", "airbnb"]},
                {"authorization": f"Bearer {token}"},
                tool="search_hotels",
            )
        assert reply["needs_api_key"] is True
        assert "single-source searches only" in reply["message"]
        assert trial_hotels.upstream.keys_seen == []
        assert await trial_hotels.trial_store.usage(SUB, utc_day()) == 0

    async def test_the_default_single_source_search_still_works(self, trial_hotels):
        """The refusal must be scoped to the multi-source paths. The ordinary
        hotel search is exactly what the allowance is for."""
        async with Session(trial_hotels) as session:
            token = await _bearer(session.http)
            reply = await call_tool(
                session.http,
                "/mcp",
                self.COMPARE_ARGS,
                {"authorization": f"Bearer {token}"},
                tool="search_hotels",
            )
        assert "needs_api_key" not in reply
        assert trial_hotels.upstream.keys_seen == [TRIAL_KEY]
        assert reply["trial"]["used_today"] == 1
