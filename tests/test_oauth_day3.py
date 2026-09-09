"""
Day 3 of MCP-protocol OAuth: registration hygiene, refresh-token reuse
detection, rate limits, CIMD client ids, and the two small fixes that ride
with them (the signed-in-with-no-key reply, and the favicon).

Everything here is driven the way day 2's tests are -- raw HTTP against the
Vercel entrypoint, through `tests/test_oauth.py`'s harness -- because every
one of these behaviours lives in the request path and a unit test of the
function would prove the function, not the endpoint.

The properties worth stating, because they are the ones that would matter if
they broke:

* **A registration cap is a cap on rows, not on sign-ins.** Nothing here may
  refuse a client that a human has already approved, and the sweep must never
  delete a registration with a live token behind it.
* **A rotated refresh token coming back kills the whole family.** Same
  `invalid_grant` either way; the difference is what happens to every other
  token from that authorization.
* **A CIMD client_id is fetched, checked and never written down.** The
  redirect_uri must be in the document, the URL must be https and public, and
  a redirect must not be followed.
"""

import time
from dataclasses import replace

import httpx
import pytest

import src.cimd as cimd
import src.oauth as oauth_module
import src.ratelimit as ratelimit
from src.legal import page
from src.oauth import (
    ACCESS_TOKEN_PREFIX,
    REFRESH_TOKEN_PREFIX,
    OAuthSupport,
    pkce_challenge,
)
from src.oauthstore import (
    AuthCode,
    MemoryOAuthStore,
    OAuthClient,
    TokenRecord,
    hash_secret,
)
from src.output_schema import FLIGHTS_OUTPUT_SCHEMA, HOTELS_OUTPUT_SCHEMA
from tests.test_connect import connected, make_settings, unconfigured, web  # noqa: F401
from tests.test_oauth import (
    EMAIL,
    MCP_HEADERS,
    MCP_OAUTH_PATH,
    ORIGIN,
    REDIRECT_URI,
    SUB,
    VERIFIER,
    Session,
    _build,
    _granted_access_token,
    authorize_query,
    call_tool,
    field,
    query_of,
    register_client,
    sign_in,
)

CIMD_ID = "https://client.test/oauth-client"


@pytest.fixture
def live(monkeypatch):
    return _build(monkeypatch)


# ── 1. dynamic registration is capped ────────────────────────────────────


class TestRegistrationCaps:
    """Open registration, as the MCP spec requires, is not unlimited
    registration. Two caps, counted in the store so every instance agrees."""

    async def test_one_address_is_capped_per_day(self, live, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", "2")
        async with Session(live) as session:
            headers = {"x-forwarded-for": "203.0.113.9"}
            for _ in range(2):
                response = await session.http.post(
                    "/oauth/register",
                    json={"redirect_uris": [REDIRECT_URI]},
                    headers=headers,
                )
                assert response.status_code == 201
            refused = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers=headers,
            )
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "3600"
        assert refused.json()["error"] == "temporarily_unavailable"

    async def test_another_address_is_not_punished_for_it(self, live, monkeypatch):
        """The per-address cap must not become a global one by accident."""
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", "1")
        async with Session(live) as session:
            first = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers={"x-forwarded-for": "203.0.113.9"},
            )
            second = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers={"x-forwarded-for": "198.51.100.4"},
            )
        assert (first.status_code, second.status_code) == (201, 201)

    async def test_the_whole_server_is_capped_per_day(self, live, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_DAY", "2")
        async with Session(live) as session:
            for ip in ("203.0.113.1", "203.0.113.2"):
                assert (
                    await session.http.post(
                        "/oauth/register",
                        json={"redirect_uris": [REDIRECT_URI]},
                        headers={"x-forwarded-for": ip},
                    )
                ).status_code == 201
            refused = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers={"x-forwarded-for": "203.0.113.3"},
            )
        assert refused.status_code == 429
        assert refused.headers["retry-after"]

    async def test_a_malformed_cap_keeps_the_default(self, live, monkeypatch):
        """"0" and "banana" must not read as "no limit"."""
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_DAY", "0")
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", "banana")
        assert oauth_module._cap("MCP_OAUTH_DCR_MAX_PER_DAY", 500) == 500
        assert oauth_module._cap("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", 30) == 30
        async with Session(live) as session:
            response = await session.http.post(
                "/oauth/register", json={"redirect_uris": [REDIRECT_URI]}
            )
        assert response.status_code == 201

    async def test_the_global_cap_is_not_a_lever_against_us(self):
        """A global cap near real traffic is a denial-of-service primitive.

        Whoever can vary the address the per-address cap is keyed on walks a
        low global counter up in minutes, and every legitimate client -- a
        new Claude or Cursor user -- is then refused for a day. So the global
        number is a backstop against unbounded rows, an order of magnitude
        above anything real, and the number that gets attention only logs.
        """
        assert oauth_module.DCR_MAX_PER_DAY >= 5_000
        assert oauth_module.DCR_WARN_PER_DAY < oauth_module.DCR_MAX_PER_DAY
        assert oauth_module.DCR_MAX_PER_IP_PER_DAY < oauth_module.DCR_WARN_PER_DAY

    async def test_the_warn_threshold_logs_and_refuses_nothing(
        self, live, monkeypatch, caplog
    ):
        monkeypatch.setenv("MCP_OAUTH_DCR_WARN_PER_DAY", "1")
        caplog.set_level("WARNING")
        async with Session(live) as session:
            for ip in ("203.0.113.31", "203.0.113.32"):
                assert (
                    await session.http.post(
                        "/oauth/register",
                        json={"redirect_uris": [REDIRECT_URI]},
                        headers={"x-forwarded-for": ip},
                    )
                ).status_code == 201
        assert "registration volume" in caplog.text

    async def test_rotating_the_forwarded_header_does_not_escape_the_cap(
        self, live, monkeypatch
    ):
        """M4, at the endpoint: the per-address cap is only a cap if the
        address cannot be chosen by the caller. `x-real-ip` is the platform's
        own header, so a rotating `x-forwarded-for` buys nothing."""
        monkeypatch.setenv("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", "2")
        async with Session(live) as session:
            statuses = []
            for forged in ("203.0.113.41", "203.0.113.42", "203.0.113.43"):
                response = await session.http.post(
                    "/oauth/register",
                    json={"redirect_uris": [REDIRECT_URI]},
                    headers={
                        "x-real-ip": "198.51.100.40",
                        "x-forwarded-for": f"{forged}, 10.0.0.1",
                    },
                )
                statuses.append(response.status_code)
        assert statuses == [201, 201, 429]

    async def test_the_recorded_address_is_the_platforms_not_the_callers(self, live):
        async with Session(live) as session:
            response = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers={
                    "x-real-ip": "198.51.100.50",
                    "x-forwarded-for": "203.0.113.50",
                },
            )
        client = await live.oauth_store.get_client(response.json()["client_id"])
        assert client.registered_ip == "198.51.100.50"

    async def test_the_registration_records_the_address(self, live):
        async with Session(live) as session:
            response = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"},
            )
        client = await live.oauth_store.get_client(response.json()["client_id"])
        # The LAST entry that can be a caller: `10.0.0.1` is a hop inside
        # somebody's network, so the address in front of it is the caller.
        assert client.registered_ip == "203.0.113.9"


# ── 2. the sweep ─────────────────────────────────────────────────────────


def _support(store) -> OAuthSupport:
    class _Auth:
        session_secret = b"k" * 32

    return OAuthSupport(store=store, auth=_Auth(), origin=ORIGIN, product="flights")


class TestTheSweep:
    """What "purge clients that never completed an authorization" means, and
    what it must never take with it."""

    async def test_it_removes_an_abandoned_registration(self):
        store = MemoryOAuthStore()
        old = time.time() - 48 * 3600
        await store.register_client(
            OAuthClient(
                client_id="fpcl_old",
                client_name="probe",
                redirect_uris=(REDIRECT_URI,),
                created_at=old,
            )
        )
        removed = await store.purge_stale_clients(time.time() - 24 * 3600)
        assert removed == 1
        assert await store.get_client("fpcl_old") is None

    async def test_it_keeps_a_registration_from_the_last_day(self):
        store = MemoryOAuthStore()
        await store.register_client(
            OAuthClient(
                client_id="fpcl_new",
                client_name="probe",
                redirect_uris=(REDIRECT_URI,),
                created_at=time.time() - 60,
            )
        )
        assert await store.purge_stale_clients(time.time() - 24 * 3600) == 0
        assert await store.get_client("fpcl_new") is not None

    async def test_it_keeps_a_registration_that_was_authorized(self):
        store = MemoryOAuthStore()
        old = time.time() - 48 * 3600
        await store.register_client(
            OAuthClient(
                client_id="fpcl_used",
                client_name="Cursor",
                redirect_uris=(REDIRECT_URI,),
                created_at=old,
            )
        )
        await store.mark_client_authorized("fpcl_used", old)
        assert await store.purge_stale_clients(time.time() - 24 * 3600) == 0
        assert await store.get_client("fpcl_used") is not None

    async def test_it_keeps_a_registration_that_still_has_a_token(self):
        """The belt-and-braces clause: rows written before the column existed
        carry no `last_authorized_at`, and deleting a client whose user is
        still signed in would be an outage for that user."""
        store = MemoryOAuthStore()
        old = time.time() - 48 * 3600
        await store.register_client(
            OAuthClient(
                client_id="fpcl_legacy",
                client_name="Claude",
                redirect_uris=(REDIRECT_URI,),
                created_at=old,
            )
        )
        await store.put_token(
            TokenRecord(
                token_hash="h",
                kind="access",
                client_id="fpcl_legacy",
                user_sub=SUB,
                provider="google",
                scope="s",
                resource=f"{ORIGIN}{MCP_OAUTH_PATH}",
                expires_at=time.time() + 3600,
            )
        )
        assert await store.purge_stale_clients(time.time() - 24 * 3600) == 0
        assert await store.get_client("fpcl_legacy") is not None

    async def test_expired_codes_and_tokens_go(self):
        store = MemoryOAuthStore()
        past = time.time() - 60
        await store.put_code(
            AuthCode(
                code_hash="c",
                client_id="fpcl_x",
                redirect_uri=REDIRECT_URI,
                code_challenge="x",
                scope="s",
                user_sub=SUB,
                provider="google",
                resource="",
                expires_at=past,
            )
        )
        await store.put_token(
            TokenRecord(
                token_hash="t",
                kind="access",
                client_id="fpcl_x",
                user_sub=SUB,
                provider="google",
                scope="s",
                resource="",
                expires_at=past,
            )
        )
        assert await store.purge_expired() == 2

    async def test_registration_runs_the_sweep_at_most_once_per_interval(self):
        store = MemoryOAuthStore()
        support = _support(store)
        old = time.time() - oauth_module.STALE_CLIENT_SECONDS - 3600
        await store.register_client(
            OAuthClient(
                client_id="fpcl_old",
                client_name="probe",
                redirect_uris=(REDIRECT_URI,),
                created_at=old,
            )
        )
        oauth_module.reset_sweep_clock()
        await support.register({"redirect_uris": [REDIRECT_URI]}, ip="203.0.113.9")
        assert await store.get_client("fpcl_old") is None

        # A second abandoned row, and a registration seconds later: the sweep
        # is throttled, so the row survives until the interval rolls.
        await store.register_client(
            OAuthClient(
                client_id="fpcl_old2",
                client_name="probe",
                redirect_uris=(REDIRECT_URI,),
                created_at=old,
            )
        )
        await support.register({"redirect_uris": [REDIRECT_URI]}, ip="203.0.113.9")
        assert await store.get_client("fpcl_old2") is not None

    async def test_a_sweep_failure_never_fails_a_registration(self):
        class Failing(MemoryOAuthStore):
            async def purge_expired(self, now=None):
                raise oauth_module.OAuthStoreError("down")

        store = Failing()
        oauth_module.reset_sweep_clock()
        issued = await _support(store).register({"redirect_uris": [REDIRECT_URI]})
        assert issued["client_id"].startswith("fpcl_")

    async def test_an_approved_client_is_stamped(self):
        """`issue_code` is what makes a registration permanent."""
        store = MemoryOAuthStore()
        support = _support(store)
        await store.register_client(
            OAuthClient(
                client_id="fpcl_a",
                client_name="Cursor",
                redirect_uris=(REDIRECT_URI,),
                created_at=time.time() - 48 * 3600,
            )
        )
        await support.issue_code(
            {
                "client_id": "fpcl_a",
                "redirect_uri": REDIRECT_URI,
                "code_challenge": pkce_challenge(VERIFIER),
                "scope": "flightpowers:search",
                "resource": f"{ORIGIN}{MCP_OAUTH_PATH}",
            },
            SUB,
            email=EMAIL,
        )
        assert await store.purge_stale_clients(time.time() - 24 * 3600) == 0


class TestTheConsentStamp:
    """M2 from the day-3 review: the sweep could delete a client in the
    middle of that client's first authorization.

    Until this, `last_authorized_at` was only written when the human pressed
    Approve. An MCP client typically registers when it is installed and is
    authorized whenever the person next opens the app, so between the two
    there is a row with no code, no token and no stamp -- exactly what the
    sweep deletes. The exchange then failed `invalid_client` with nothing in
    the logs naming the sweep.
    """

    async def _old_registration(self, live, http, age: float) -> str:
        _, registered = await register_client(http)
        client_id = registered["client_id"]
        stored = await live.oauth_store.get_client(client_id)
        await live.oauth_store.register_client(
            replace(stored, created_at=time.time() - age)
        )
        return client_id

    async def test_a_rendered_consent_page_saves_the_row_from_the_sweep(self, live):
        age = oauth_module.STALE_CLIENT_SECONDS + 3600
        async with Session(live) as session:
            http = session.http
            client_id = await self._old_registration(live, http, age)
            await sign_in(http)
            page_ = await http.get(f"/connect/authorize?{authorize_query(client_id)}")
            assert page_.status_code == 200

            swept = await live.oauth_store.purge_stale_clients(
                time.time() - oauth_module.STALE_CLIENT_SECONDS
            )
        assert swept == 0
        assert await live.oauth_store.get_client(client_id) is not None

    async def test_without_that_render_the_same_row_is_swept(self, live):
        """The control. Without the stamp the row above is litter, and the
        test above would pass for the wrong reason."""
        age = oauth_module.STALE_CLIENT_SECONDS + 3600
        async with Session(live) as session:
            client_id = await self._old_registration(live, session.http, age)
            swept = await live.oauth_store.purge_stale_clients(
                time.time() - oauth_module.STALE_CLIENT_SECONDS
            )
        assert swept == 1
        assert await live.oauth_store.get_client(client_id) is None

    async def test_the_window_is_a_week_not_a_day(self):
        """A client installed on Monday and opened on Thursday still works."""
        assert oauth_module.STALE_CLIENT_SECONDS >= 7 * 24 * 3600
        store = MemoryOAuthStore()
        await store.register_client(
            OAuthClient(
                client_id="fpcl_three_days",
                client_name="Cursor",
                redirect_uris=(REDIRECT_URI,),
                created_at=time.time() - 3 * 24 * 3600,
            )
        )
        removed = await store.purge_stale_clients(
            time.time() - oauth_module.STALE_CLIENT_SECONDS
        )
        assert removed == 0

    async def test_a_cimd_client_has_no_row_and_is_not_stamped(self):
        """A CIMD client_id is a URL, never a registration. Stamping one
        would be an UPDATE that matches nothing; the guard is here so it
        stays that way if `mark_client_authorized` ever grows an upsert."""
        calls: list[str] = []

        class Watching(MemoryOAuthStore):
            async def mark_client_authorized(self, client_id, now=None):
                calls.append(client_id)

        support = _support(Watching())
        await support.note_consent_shown(CIMD_ID)
        await support.note_consent_shown("")
        assert calls == []
        await support.note_consent_shown("fpcl_real")
        assert calls == ["fpcl_real"]

    async def test_a_store_failure_does_not_stop_the_page(self):
        class Failing(MemoryOAuthStore):
            async def mark_client_authorized(self, client_id, now=None):
                raise oauth_module.OAuthStoreError("down")

        await _support(Failing()).note_consent_shown("fpcl_real")


# ── 3. refresh-token reuse ───────────────────────────────────────────────


class TestRefreshReuse:
    async def _pair(self, http):
        _, registered = await register_client(http)
        client_id = registered["client_id"]
        await sign_in(http)
        page_ = await http.get(f"/connect/authorize?{authorize_query(client_id)}")
        approved = await http.post(
            "/connect/authorize",
            data={
                "csrf": field(page_.text, "csrf"),
                "request": field(page_.text, "request"),
                "decision": "approve",
            },
        )
        code = query_of(approved.headers["location"])["code"]
        tokens = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": VERIFIER,
            },
        )
        assert tokens.status_code == 200, tokens.text
        return client_id, tokens.json()

    async def test_a_replayed_refresh_token_revokes_the_family(self, live):
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            rotated = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
            second = rotated.json()

            # The first replay is the benign retry, and is answered with the
            # pair rotation already issued. See TestTheRetryGrace.
            retry = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert retry.status_code == 200

            replay = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert replay.status_code == 400
            assert replay.json()["error"] == "invalid_grant"

            # Everything descended from that one authorization is gone: the
            # access token the thief would have got, and the honest client's
            # current pair.
            for token in (first["access_token"], second["access_token"]):
                response = await http.post(
                    MCP_OAUTH_PATH,
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    headers={**MCP_HEADERS, "authorization": f"Bearer {token}"},
                )
                assert response.status_code == 401
            dead = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": second["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert dead.status_code == 400

    async def test_an_unused_refresh_token_still_rotates_normally(self, live):
        """The false-positive check: reuse detection must not fire on the
        ordinary case, or every client is logged out every hour."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            second = (
                await http.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": first["refresh_token"],
                        "client_id": client_id,
                    },
                )
            ).json()
            third = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": second["refresh_token"],
                    "client_id": client_id,
                },
            )
        assert third.status_code == 200
        assert third.json()["access_token"].startswith(ACCESS_TOKEN_PREFIX)
        assert third.json()["refresh_token"].startswith(REFRESH_TOKEN_PREFIX)

    async def test_a_token_that_never_existed_revokes_nothing(self, live):
        """An invented refresh token is a wrong guess, not a compromise: it
        must not be able to log anybody out."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            response = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": f"{REFRESH_TOKEN_PREFIX}invented",
                    "client_id": client_id,
                },
            )
            assert response.status_code == 400
            still = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
        assert still.status_code == 200

    async def test_the_rotated_row_is_kept_and_stamped(self, live):
        """Reuse detection is only possible because rotation stops DELETING
        the row. If this ever goes back to a delete, the test above passes for
        the wrong reason."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
        row = await live.oauth_store.get_token_any(
            hash_secret(first["refresh_token"]), "refresh"
        )
        assert row is not None
        assert row.revoked_at is not None
        assert row.family_id

    async def test_revoking_a_refresh_token_takes_its_access_token(self, live):
        """RFC 7009 2.1: revoking a refresh token SHOULD revoke what it
        issued. The family id is exactly that set."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            revoked = await http.post(
                "/oauth/revoke",
                data={"token": first["refresh_token"], "client_id": client_id},
            )
            assert revoked.status_code == 200
            response = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {first['access_token']}",
                },
            )
        assert response.status_code == 401


    async def test_a_client_cannot_revoke_another_clients_token(self, live):
        """RFC 7009 2.1: the server validates that the token was issued to
        the client asking. Without that, any registered client that gets hold
        of a token can sign that user out -- and with family revocation the
        blast radius is the whole authorization. Still 200 either way (2.2),
        so the caller learns nothing."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            _, other = await register_client(http)
            assert other["client_id"] != client_id

            answered = await http.post(
                "/oauth/revoke",
                data={
                    "token": first["refresh_token"],
                    "client_id": other["client_id"],
                },
            )
            assert answered.status_code == 200

            alive = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {first['access_token']}",
                },
            )
            assert alive.status_code == 200
            # And the owner can still revoke it.
            mine = await http.post(
                "/oauth/revoke",
                data={"token": first["refresh_token"], "client_id": client_id},
            )
            assert mine.status_code == 200
            gone = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {first['access_token']}",
                },
            )
        assert gone.status_code == 401


class TestTheRetryGrace:
    """M5 from the day-3 review: reuse detection cost a legitimate user their
    session on an ordinary retry.

    Rotation plus family revocation is right, and it has one ugly edge: a
    client whose refresh response never arrived retries with the token it
    still holds, and the whole family dies. On a flaky link that is a
    re-sign-in with no explanation. So the FIRST replay, within seconds, from
    the same client, is answered with the pair that rotation already issued.
    Nothing new is created; a second replay, or a late one, still kills the
    family.
    """

    async def _pair(self, http):
        return await TestRefreshReuse._pair(self, http)

    async def test_a_retry_inside_the_window_returns_the_same_pair(self, live):
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            rotated = (
                await http.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": first["refresh_token"],
                        "client_id": client_id,
                    },
                )
            ).json()
            retry = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert retry.status_code == 200
            assert retry.json()["access_token"] == rotated["access_token"]
            assert retry.json()["refresh_token"] == rotated["refresh_token"]

            # And the pair still works, which is the whole point: the user
            # was not signed out by a dropped response.
            alive = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {rotated['access_token']}",
                },
            )
        assert alive.status_code == 200

    async def test_a_replay_after_the_window_still_kills_the_family(self, live):
        """The grace is a few seconds of memory on one instance. Once it is
        gone -- the window rolled, or the retry landed on another instance --
        a rotated token coming back is treated as theft again."""
        async with Session(live) as session:
            http = session.http
            client_id, first = await self._pair(http)
            rotated = (
                await http.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": first["refresh_token"],
                        "client_id": client_id,
                    },
                )
            ).json()
            oauth_module.reset_replay_grace()  # the window has passed

            replay = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert replay.status_code == 400
            assert replay.json()["error"] == "invalid_grant"

            dead = await http.post(
                MCP_OAUTH_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    **MCP_HEADERS,
                    "authorization": f"Bearer {rotated['access_token']}",
                },
            )
        assert dead.status_code == 401

    def test_the_window_is_spent_once_and_belongs_to_one_client(self):
        oauth_module.reset_replay_grace()
        issued = {"access_token": "fpa_x", "refresh_token": "fpr_x"}
        oauth_module._remember_rotation("hash-1", "fpcl_a", issued, 1000.0)
        # Another client presenting the same token gets nothing...
        assert oauth_module._take_replay("hash-1", "fpcl_b", 1001.0) is None
        # ...and the entry is spent either way, so a wrong client cannot
        # probe for the existence of one.
        oauth_module._remember_rotation("hash-2", "fpcl_a", issued, 1000.0)
        assert oauth_module._take_replay("hash-2", "fpcl_a", 1001.0) is issued
        assert oauth_module._take_replay("hash-2", "fpcl_a", 1001.0) is None
        # Late is not a retry.
        oauth_module._remember_rotation("hash-3", "fpcl_a", issued, 1000.0)
        late = 1000.0 + oauth_module.REFRESH_REPLAY_GRACE_SECONDS + 1
        assert oauth_module._take_replay("hash-3", "fpcl_a", late) is None

    def test_the_grace_table_cannot_grow_without_limit(self):
        oauth_module.reset_replay_grace()
        for n in range(oauth_module._REPLAY_MAX + 50):
            oauth_module._remember_rotation(f"h{n}", "fpcl_a", {}, 1000.0)
        assert len(oauth_module._REPLAY) <= oauth_module._REPLAY_MAX
        oauth_module.reset_replay_grace()


# ── 4. rate limits ───────────────────────────────────────────────────────


class TestRateLimits:
    async def test_register_is_rate_limited(self, live):
        async with Session(live) as session:
            headers = {"x-forwarded-for": "203.0.113.77"}
            for _ in range(ratelimit.REGISTER.count):
                assert (
                    await session.http.post(
                        "/oauth/register",
                        json={"redirect_uris": [REDIRECT_URI]},
                        headers=headers,
                    )
                ).status_code == 201
            refused = await session.http.post(
                "/oauth/register",
                json={"redirect_uris": [REDIRECT_URI]},
                headers=headers,
            )
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == str(ratelimit.REGISTER.retry_after())
        assert refused.json()["error"] == "temporarily_unavailable"

    async def test_the_token_endpoint_is_rate_limited(self, live):
        async with Session(live) as session:
            headers = {"x-forwarded-for": "203.0.113.78"}
            for _ in range(ratelimit.TOKEN.count):
                await session.http.post(
                    "/oauth/token", data={"grant_type": "refresh_token"}, headers=headers
                )
            refused = await session.http.post(
                "/oauth/token", data={"grant_type": "refresh_token"}, headers=headers
            )
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) >= 1

    @pytest.mark.asyncio
    async def test_connect_save_is_rate_limited(self, connected):
        """Every save spends one request from the user's own plan, so this
        one protects their money, not just our database."""
        mcp, _, _ = connected
        async with web(mcp) as http:
            await sign_in(http)
            for _ in range(ratelimit.CONNECT_SAVE.count):
                # A bad csrf still counts: the limit is checked first, on
                # purpose, so a flood cannot be waved through by malforming it.
                assert (
                    await http.post("/connect/save", data={"csrf": "nope"})
                ).status_code == 200
            refused = await http.post("/connect/save", data={"csrf": "nope"})
            # LOW #1 from the day-3 review: the header said an hour and the
            # page said a few minutes. The header is the true number, so the
            # copy moved to it -- a user who reads "a few minutes", comes
            # back and is refused again learns not to believe the page.
            assert "wait an hour" in refused.text
            # LOW #2: keyed on the signed-in account, not the address. The
            # caller is already authenticated, and an office behind one NAT
            # would otherwise share ten saves an hour between everybody.
            elsewhere = await http.post(
                "/connect/save",
                data={"csrf": "nope"},
                headers={"x-real-ip": "203.0.113.99"},
            )
            assert elsewhere.status_code == 429
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == str(
            ratelimit.CONNECT_SAVE.retry_after()
        )
        assert "lot of saves" in refused.text

    def test_a_different_address_has_its_own_budget(self):
        limiter = ratelimit.RateLimiter()
        limit = ratelimit.Limit("t", 2, 60)
        assert limiter.allow(limit, "a") and limiter.allow(limit, "a")
        assert not limiter.allow(limit, "a")
        assert limiter.allow(limit, "b")

    def test_the_window_rolls(self):
        limiter = ratelimit.RateLimiter()
        limit = ratelimit.Limit("t", 1, 60)
        assert limiter.allow(limit, "a", now=1000.0)
        assert not limiter.allow(limit, "a", now=1030.0)
        assert limiter.allow(limit, "a", now=1061.0)

    def test_a_full_table_fails_closed(self):
        """Memory is finite on a serverless instance, and a defensive
        dictionary that grows without limit is its own denial of service. Past
        the cap the answer is 'no', never 'let it through'."""
        limiter = ratelimit.RateLimiter(max_keys=2)
        limit = ratelimit.Limit("t", 5, 60)
        assert limiter.allow(limit, "a", now=1000.0)
        assert limiter.allow(limit, "b", now=1000.0)
        assert not limiter.allow(limit, "c", now=1000.0)

    def test_callers_with_no_address_share_one_bucket(self):
        """Bucketing unknowns separately would be the same as not limiting
        them, since the address is the thing being withheld."""
        limiter = ratelimit.RateLimiter()
        limit = ratelimit.Limit("t", 1, 60)
        assert limiter.allow(limit, "")
        assert not limiter.allow(limit, "")

    def test_the_address_comes_from_the_platform_not_the_caller(self):
        """`x-real-ip` first, then the LAST usable forwarded hop.

        Entry 0 of `x-forwarded-for` is whatever the caller wrote; proxies
        append on the right. Reading it -- which this did until the day-3
        review -- makes every limit here a header away from being bypassed.
        """
        # A real chain: our proxy appended the internal hop, the public
        # address in front of it is the caller.
        assert ratelimit.client_ip({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}) == "1.2.3.4"
        assert ratelimit.client_ip({"x-real-ip": "5.6.7.8"}) == "5.6.7.8"
        # A forged entry 0 loses to the platform's own header...
        assert (
            ratelimit.client_ip(
                {"x-real-ip": "5.6.7.8", "x-forwarded-for": "9.9.9.9, 5.6.7.8"}
            )
            == "5.6.7.8"
        )
        # ...and loses to the right-most hop when there is no other header.
        assert (
            ratelimit.client_ip({"x-forwarded-for": "9.9.9.9, 1.2.3.4"}) == "1.2.3.4"
        )
        # Nothing usable is ONE bucket, not a bucket of the caller's choice.
        assert ratelimit.client_ip({}) == ratelimit.UNKNOWN_IP
        assert (
            ratelimit.client_ip({"x-forwarded-for": "not-an-address, 10.0.0.1"})
            == ratelimit.UNKNOWN_IP
        )
        assert (
            ratelimit.client_ip({"x-real-ip": "unknown", "x-forwarded-for": "127.0.0.1"})
            == ratelimit.UNKNOWN_IP
        )

    def test_an_internal_hop_is_never_the_caller(self):
        assert ratelimit.usable_address("93.184.216.34") == "93.184.216.34"
        for internal in (
            "10.0.0.1",
            "172.16.9.9",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "example.com",
            "",
        ):
            assert ratelimit.usable_address(internal) == "", internal


# ── 5. client id metadata documents ──────────────────────────────────────


def _cimd_transport(monkeypatch, handler, resolves_to=("93.184.216.34",)):
    """Point every CIMD fetch at `handler` instead of the network.

    The resolver is stubbed too: the example hostnames here do not exist, and
    the address check that runs before the fetch is a real one -- see
    `check_host_addresses`. `resolves_to` is what these fake names answer
    with, so a test can also say "this name points at 127.0.0.1".
    """
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def _resolve(_host: str):
        return tuple(resolves_to)

    monkeypatch.setattr(cimd, "resolve_host", _resolve)
    monkeypatch.setattr(
        cimd,
        "build_http_client",
        lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(_wrapped)),
    )
    return seen


def _document(**overrides):
    body = {
        "client_id": CIMD_ID,
        "client_name": "Smithery",
        "redirect_uris": [REDIRECT_URI],
    }
    body.update(overrides)
    return lambda _request: httpx.Response(200, json=body)


class TestClientIdMetadataDocuments:
    async def test_the_metadata_advertises_it(self, live):
        async with Session(live) as session:
            body = (
                await session.http.get("/.well-known/oauth-authorization-server")
            ).json()
        assert body["client_id_metadata_document_supported"] is True
        # DCR is still offered. This is an additional shape, not a swap.
        assert body["registration_endpoint"].endswith("/oauth/register")

    async def test_a_cimd_client_can_sign_a_user_in(self, live, monkeypatch):
        seen = _cimd_transport(monkeypatch, _document())
        await live.key_store.put(SUB, EMAIL, "user-key-" + "a" * 36)
        async with Session(live) as session:
            http = session.http
            await sign_in(http)
            consent = await http.get(f"/connect/authorize?{authorize_query(CIMD_ID)}")
            assert consent.status_code == 200, consent.text[:400]
            assert "Smithery" in consent.text
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(consent.text, "csrf"),
                    "request": field(consent.text, "request"),
                    "decision": "approve",
                },
            )
            code = query_of(approved.headers["location"])["code"]
            tokens = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CIMD_ID,
                    "code_verifier": VERIFIER,
                },
            )
            assert tokens.status_code == 200, tokens.text
            result = await call_tool(
                http,
                MCP_OAUTH_PATH,
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
                {"authorization": f"Bearer {tokens.json()['access_token']}"},
            )
        assert result["result_count"] == 1
        assert seen, "the document was never fetched"

    async def test_nothing_is_written_down_for_it(self, live, monkeypatch):
        _cimd_transport(monkeypatch, _document())
        async with Session(live) as session:
            await sign_in(session.http)
            await session.http.get(f"/connect/authorize?{authorize_query(CIMD_ID)}")
        # The whole point: no registration row, so nothing to cap and nothing
        # to sweep.
        assert await live.oauth_store.count_clients_since(0) == 0

    async def test_an_unlisted_redirect_uri_is_refused(self, live, monkeypatch):
        _cimd_transport(monkeypatch, _document())
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                "/connect/authorize?"
                + authorize_query(CIMD_ID, redirect_uri="http://127.0.0.1:9/evil")
            )
        assert response.status_code == 400
        assert "not listed" in response.text

    async def test_a_document_naming_another_client_id_is_refused(
        self, live, monkeypatch
    ):
        _cimd_transport(monkeypatch, _document(client_id="https://elsewhere.test/c"))
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400
        assert "different client_id" in response.text

    @pytest.mark.parametrize(
        "client_id",
        [
            "http://client.test/oauth-client",
            "https://localhost/oauth-client",
            "https://127.0.0.1/oauth-client",
            "https://client/oauth-client",
        ],
    )
    async def test_an_unsafe_client_id_url_is_never_fetched(
        self, live, monkeypatch, client_id
    ):
        """http is refused because the claim would be whatever the network
        says; a private or bare host is refused because fetching it would make
        this endpoint a request-forgery primitive."""
        seen = _cimd_transport(monkeypatch, _document())
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(client_id)}"
            )
        assert response.status_code == 400
        assert seen == []

    async def test_a_redirect_is_not_followed(self, live, monkeypatch):
        _cimd_transport(
            monkeypatch,
            lambda _r: httpx.Response(302, headers={"location": "https://evil.test/d"}),
        )
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400
        assert "redirect" in response.text

    async def test_an_oversized_document_is_refused(self, live, monkeypatch):
        _cimd_transport(
            monkeypatch,
            lambda _r: httpx.Response(200, content=b"{" + b"x" * (70 * 1024)),
        )
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400
        assert "too large" in response.text

    async def test_a_document_with_no_redirect_uris_is_refused(
        self, live, monkeypatch
    ):
        _cimd_transport(monkeypatch, _document(redirect_uris=[]))
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400

    async def test_a_document_that_stops_resolving_fails_the_exchange(
        self, live, monkeypatch
    ):
        """The token endpoint resolves the client_id too, so a document that
        goes away between authorize and exchange is a 401, not a token."""
        _cimd_transport(monkeypatch, _document())
        async with Session(live) as session:
            http = session.http
            await sign_in(http)
            consent = await http.get(f"/connect/authorize?{authorize_query(CIMD_ID)}")
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(consent.text, "csrf"),
                    "request": field(consent.text, "request"),
                    "decision": "approve",
                },
            )
            code = query_of(approved.headers["location"])["code"]
            cimd.clear_cache()
            _cimd_transport(monkeypatch, lambda _r: httpx.Response(404))
            tokens = await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CIMD_ID,
                    "code_verifier": VERIFIER,
                },
            )
        assert tokens.status_code == 401
        assert tokens.json()["error"] == "invalid_client"

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",       # loopback behind an ordinary-looking name
            "169.254.169.254",  # the cloud metadata address
            "10.1.2.3",         # RFC1918
            "::1",              # loopback, v6
            "::ffff:127.0.0.1",  # loopback wearing a v6 hat
        ],
    )
    async def test_a_public_name_pointing_at_a_private_address_is_refused(
        self, live, monkeypatch, address
    ):
        """The name check is not the control; the address is.

        `127.0.0.1.nip.io` is a real hostname with a dot in it that resolves
        to loopback. Without resolving the name, this endpoint -- which any
        stranger can reach, signed in or not -- is an outbound fetcher
        pointed wherever the caller likes.
        """
        seen = _cimd_transport(monkeypatch, _document(), resolves_to=(address,))
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400
        assert "public address" in response.text
        assert seen == [], "the socket must never be opened"

    async def test_a_name_that_does_not_resolve_is_refused(self, live, monkeypatch):
        seen = _cimd_transport(monkeypatch, _document(), resolves_to=())
        async with Session(live) as session:
            await sign_in(session.http)
            response = await session.http.get(
                f"/connect/authorize?{authorize_query(CIMD_ID)}"
            )
        assert response.status_code == 400
        assert seen == []

    async def test_the_cimd_lookup_is_rate_limited(self, live, monkeypatch):
        """The one unauthenticated outbound fetch in this server has a ceiling.

        A registered `fpcl_` client is not affected: it never touches the
        network, so it is not counted here.
        """
        _cimd_transport(monkeypatch, _document())
        ratelimit.LIMITER.reset()
        async with Session(live) as session:
            http = session.http
            await sign_in(http)
            last = None
            for _ in range(ratelimit.CIMD_FETCH.count + 1):
                last = await http.get(f"/connect/authorize?{authorize_query(CIMD_ID)}")
        assert last.status_code == 429

    def test_a_public_address_is_allowed(self):
        assert cimd._is_public_address("93.184.216.34")
        assert cimd._is_public_address("2606:4700:4700::1111")
        assert not cimd._is_public_address("192.168.0.1")
        assert not cimd._is_public_address("not-an-address")

    def test_what_counts_as_a_cimd_client_id(self):
        assert cimd.is_cimd_client_id(CIMD_ID)
        assert not cimd.is_cimd_client_id("fpcl_abc")
        assert not cimd.is_cimd_client_id("http://client.test/c")
        assert not cimd.is_cimd_client_id("")


# ── 6. the signed-in-with-no-key reply ───────────────────────────────────


class TestSignedInWithNoKey:
    async def test_it_names_the_account_and_the_connect_page(self, live):
        async with Session(live) as session:
            http = session.http
            access = await _granted_access_token(http)
            reply = await call_tool(
                http,
                MCP_OAUTH_PATH,
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
                {"authorization": f"Bearer {access}"},
            )
        assert reply["needs_api_key"] is True
        assert f"signed in as {EMAIL}" in reply["message"]
        assert f"{ORIGIN}/connect" in reply["message"]
        # Not the header ladder: this caller has already authenticated, and a
        # model would read those steps out as if they were the fix.
        steps = " ".join(reply["how_to_get_a_key"]["how"])
        assert "x-rapidapi-key" not in steps
        assert "rapidapi_key=" not in steps
        assert "/connect" in steps
        assert live.upstream.keys_seen == []

    async def test_a_disconnected_connect_token_still_gets_its_own_text(self, live):
        """The other STORE_MISS source must not be swallowed by the new one:
        someone who pressed Disconnect is told that, not "you are signed
        in"."""
        async with Session(live) as session:
            http = session.http
            token = await self._connect_token(http, live)
            await live.key_store.revoke(SUB)
            reply = await call_tool(
                http,
                "/mcp",
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
                {"authorization": f"Bearer {token}"},
            )
        assert "disconnected" in reply["message"]
        assert "signed in as" not in reply["message"]

    @staticmethod
    async def _connect_token(http, live) -> str:
        import re

        await live.key_store.put(SUB, EMAIL, "user-key-" + "b" * 36)
        await sign_in(http)
        page_ = (await http.get("/connect")).text
        return re.search(r"fp_token=(fpk_[^\s&<]+)", page_).group(1)

    async def test_the_email_header_cannot_be_forged(self, live):
        """`x-fp-oauth-email` is displayed back to a user, so a caller who
        could set it could make our own reply lie about which account is
        connected."""
        await live.key_store.put(SUB, EMAIL, "user-key-" + "c" * 36)
        async with Session(live) as session:
            reply = await call_tool(
                session.http,
                "/mcp",
                {
                    "departure_date": "2026-09-20",
                    "from_airport": "TLV",
                    "to_airport": "ATH",
                },
                {
                    "x-fp-oauth-subject": SUB,
                    "x-fp-oauth-email": "victim@example.test",
                    # A real key, so the request gets past the 2026-09-09
                    # challenge on `/mcp` and the forged headers are tested
                    # where they would do damage: at credential resolution.
                    "x-rapidapi-key": "header-key-" + "d" * 36,
                },
            )
        assert "victim@example.test" not in str(reply)
        # The caller's own key was spent, never the one stored for SUB.
        assert live.upstream.keys_seen == ["header-key-" + "d" * 36]


# ── 7. output schema portability ─────────────────────────────────────────


def _type_arrays(node, path="$"):
    """Every place a schema declares `"type": [...]` instead of `anyOf`."""
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), list):
            found.append(path)
        for key, value in node.items():
            found += _type_arrays(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _type_arrays(value, f"{path}[{i}]")
    return found


class TestOutputSchemaPortability:
    """The MCP Inspector flags a type ARRAY, and several client-side
    validators and code generators read only its first entry -- which would
    make a legitimate null look like a violation. `anyOf` with one type per
    branch says the same thing in the form everything handles."""

    def test_no_schema_declares_a_type_array(self):
        assert _type_arrays(FLIGHTS_OUTPUT_SCHEMA) == []
        assert _type_arrays(HOTELS_OUTPUT_SCHEMA) == []

    def test_cheapest_is_an_object_or_null(self):
        entry = FLIGHTS_OUTPUT_SCHEMA["properties"]["by_destination"][
            "additionalProperties"
        ]["properties"]
        assert entry["cheapest"]["anyOf"] == [
            {"type": "object", "additionalProperties": True},
            {"type": "null"},
        ]

    def test_cheapest_price_is_a_number_or_null(self):
        dates = FLIGHTS_OUTPUT_SCHEMA["properties"]["by_destination"][
            "additionalProperties"
        ]["properties"]["dates"]["additionalProperties"]["properties"]
        assert dates["cheapest_price"]["anyOf"] == [
            {"type": "number"},
            {"type": "null"},
        ]

    async def test_the_served_schema_is_the_same(self, live):
        """The schema a client actually reads, off tools/list."""
        async with Session(live) as session:
            started = await session.http.post(
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
                # `/mcp` challenges a caller with no credential at all
                # since 2026-09-09, so a schema probe brings a key like every
                # other caller does.
                headers={**MCP_HEADERS, "x-rapidapi-key": "probe-key-" + "e" * 36},
            )
            listed = await session.http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    **MCP_HEADERS,
                    "x-rapidapi-key": "probe-key-" + "e" * 36,
                    "mcp-session-id": started.headers.get("mcp-session-id", ""),
                },
            )
        import json as _json

        payload = None
        for line in listed.text.splitlines():
            if line.startswith("data:"):
                payload = _json.loads(line[5:].strip())
        payload = payload or _json.loads(listed.text)
        tools = payload["result"]["tools"]
        assert tools
        for tool in tools:
            assert _type_arrays(tool.get("outputSchema") or {}) == []


# ── 8. the favicon ───────────────────────────────────────────────────────


class TestFavicon:
    @pytest.mark.asyncio
    async def test_the_svg_is_served(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            response = await http.get("/favicon.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert response.text.startswith("<svg")

    @pytest.mark.asyncio
    async def test_the_ico_is_a_204_not_a_404(self, connected):
        """A 404 per page view is noise that hides a real one."""
        mcp, _, _ = connected
        async with web(mcp) as http:
            response = await http.get("/favicon.ico")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_every_page_points_at_it(self, connected):
        mcp, _, _ = connected
        async with web(mcp) as http:
            for path in ("/", "/privacy", "/terms", "/support", "/connect"):
                body = (await http.get(path)).text
                assert 'rel="icon"' in body, path
                assert "/favicon.svg" in body, path

    def test_the_link_is_in_the_head(self):
        assert '<link rel="icon" href="/favicon.svg"' in page("t", "<p>x</p>")
