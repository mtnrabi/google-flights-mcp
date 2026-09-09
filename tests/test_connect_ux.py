"""
What /connect says, to whom, and what it stops printing.

Two things shipped on 2026-09-08 that this file pins down, both found by a
real user on 2026-09-09 (`state/gtm/mcp-oauth-ladder-2026-09-08.md`, "Matan's
connector test"):

1. **/connect was written for one of its two audiences.** A user who added
   `…/mcp/oauth` as a connector in Claude, signed in and approved it was then
   shown, as the first thing under "Connected", a 90-day `fp_token` URL and
   the instruction "paste this into your MCP client as the server URL". That
   is the OTHER way in. The obvious next move -- paste it into the same
   client -- leaves one account with two connectors to one server.
2. **The token was printed in clear text.** It is a bearer credential for
   somebody's RapidAPI plan, and it sat on screen for every screenshot and
   screen share.

So: the page renders one of two variants, the connect URL is never in the
HTML for the OAuth variant, and in the token variant it is behind a Reveal
control. The tests below assert the ABSENCE of the token as hard as they
assert its presence -- an absence is the property that decays quietly.

The deny half is here too, because it is the same page family: pressing Deny
used to bounce the browser at a dead `127.0.0.1` port and render Chrome's own
connection-error page.
"""

import base64
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

import src.entrypoint as entrypoint
import src.keystore as keystore_module
import src.oauth as oauth_module
import src.server as server_module
from src.connect import FLOW_OAUTH, FLOW_TOKEN, signed_in_html
from src.keystore import KeySummary, MemoryKeyStore
from src.oauth import MCP_OAUTH_PATH, is_loopback_redirect, pkce_challenge
from src.oauthstore import MemoryOAuthStore, TokenRecord
from src.webauth import CONNECT_TOKEN_PREFIX, SESSION_COOKIE

ORIGIN = "https://mcp.test"
LOOPBACK_REDIRECT = "http://127.0.0.1:33418/callback"
HOSTED_REDIRECT = "https://client.example/callback"
GOOGLE_CLIENT_ID = "1234.apps.googleusercontent.com"
MASTER = bytes(range(32))
MASTER_B64 = base64.b64encode(MASTER).decode()
SUB = "sub-1"
EMAIL = "a@example.test"
USER_KEY = "user-key-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
VERIFIER = "verifier-" + "a" * 50


def _id_token() -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims = {
        "sub": SUB,
        "email": EMAIL,
        "email_verified": True,
        "aud": GOOGLE_CLIENT_ID,
    }
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def _upstream(request: httpx.Request) -> httpx.Response:
    """Google's token endpoint and RapidAPI's gateway, both said yes."""
    if str(request.url).startswith("https://oauth2.googleapis.com/token"):
        return httpx.Response(200, json={"id_token": _id_token()})
    return httpx.Response(200, json={"detail": "no dates"})


class Deployment:
    def __init__(self, app, key_store, oauth_store) -> None:
        self.app = app
        self.key_store = key_store
        self.oauth_store = oauth_store


@pytest.fixture
def live(monkeypatch):
    """A configured deployment with both stores in memory."""
    for name in (
        "MCP_PRODUCTS_BY_HOST",
        "MCP_PUBLIC_URL_FLIGHTS",
        "MCP_PUBLIC_URL_HOTELS",
        "RAPIDAPI_KEY",
        "MCP_OAUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_PRODUCTS", "flights")
    monkeypatch.setenv("MCP_PRODUCTS_BY_HOST", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{ORIGIN}/mcp")
    monkeypatch.setenv("RAPIDAPI_BASE_URL", "https://upstream.test")
    monkeypatch.setenv("RAPIDAPI_HOST", "upstream.test")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
    monkeypatch.setenv("MCP_KEY_MASTER", MASTER_B64)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused-in-tests/db")

    key_store = MemoryKeyStore(MASTER)
    oauth_store = MemoryOAuthStore()
    monkeypatch.setattr(keystore_module, "build_key_store", lambda *a, **k: key_store)
    monkeypatch.setattr(oauth_module, "build_oauth_store", lambda *a, **k: oauth_store)
    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(_upstream)),
    )
    return Deployment(entrypoint.build_entrypoint().app, key_store, oauth_store)


class Session:
    """An httpx client bound to one deployment, with its lifespan run."""

    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment
        self._lifespan = None
        self.http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Session":
        app = self.deployment.app
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.http.aclose()
        await self._lifespan.__aexit__(*exc)


async def sign_in(http, next_path: str = "") -> None:
    query = f"?{urlencode({'next': next_path})}" if next_path else ""
    start = await http.get(f"/connect/start{query}")
    assert start.status_code == 302
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    callback = await http.get(f"/connect/callback?code=code-1&state={state}")
    assert callback.status_code == 303
    assert SESSION_COOKIE in http.cookies


def grant(store: MemoryOAuthStore, **overrides) -> None:
    """A live OAuth grant for SUB: the state a connector user is in."""
    record = dict(
        token_hash="hash-" + str(time.time()),
        kind="refresh",
        client_id="fpcl_claude",
        user_sub=SUB,
        provider="google",
        scope="flightpowers:search",
        resource=f"{ORIGIN}{MCP_OAUTH_PATH}",
        expires_at=time.time() + 30 * 24 * 3600,
        user_email=EMAIL,
        family_id="fam_1",
    )
    record.update(overrides)
    store._tokens[record["token_hash"]] = TokenRecord(**record)


async def store_key(deployment: Deployment) -> None:
    await deployment.key_store.put(SUB, EMAIL, USER_KEY)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def query_of(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def register(http, redirect_uri: str = LOOPBACK_REDIRECT) -> str:
    response = await http.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def authorize_query(client_id: str, redirect_uri: str = LOOPBACK_REDIRECT) -> str:
    return urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": pkce_challenge(VERIFIER),
            "code_challenge_method": "S256",
            "scope": "flightpowers:search",
            "state": "state-123",
            "resource": f"{ORIGIN}{MCP_OAUTH_PATH}",
        }
    )


def field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"no {name} field in the page"
    return match.group(1)


# ── the page a connector user gets ───────────────────────────────────────


class TestOAuthReader:
    """Signed in, key stored, a client already connected."""

    async def test_the_page_says_you_are_set_and_prints_no_token(self, live):
        await store_key(live)
        grant(live.oauth_store)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")

        assert page.status_code == 200
        assert f"Signed in as <strong>{EMAIL}" in page.text
        assert f"…{USER_KEY[-4:]}" in page.text
        assert "You are set. There is nothing to paste." in page.text
        assert "Go back to your assistant" in page.text
        # The whole point: no bearer credential anywhere in the document.
        assert CONNECT_TOKEN_PREFIX not in page.text
        assert "fp_token" not in page.text
        assert "Paste this into your MCP client as the server URL" not in page.text
        # ...and the two controls that must still be there.
        assert "Replace it" in page.text
        assert "Disconnect" in page.text

    async def test_a_connect_url_is_one_click_away_for_another_client(self, live):
        await store_key(live)
        grant(live.oauth_store)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
            assert "/connect?token=1" in page.text
            asked = await session.http.get("/connect?token=1")

        assert CONNECT_TOKEN_PREFIX in asked.text
        assert "Your connect URL" in asked.text

    async def test_a_revoked_grant_is_not_a_grant(self, live):
        """Disconnect, then a fresh visit: back to the URL the user needs."""
        await store_key(live)
        grant(live.oauth_store, revoked_at=time.time() - 5)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert "You are set. There is nothing to paste." not in page.text
        assert CONNECT_TOKEN_PREFIX in page.text

    async def test_an_expired_grant_is_not_a_grant(self, live):
        await store_key(live)
        grant(live.oauth_store, expires_at=time.time() - 5)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert CONNECT_TOKEN_PREFIX in page.text

    async def test_a_grant_for_someone_else_is_not_a_grant(self, live):
        await store_key(live)
        grant(live.oauth_store, user_sub="sub-someone-else")
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert CONNECT_TOKEN_PREFIX in page.text

    async def test_the_session_flow_alone_is_enough_before_any_token_exists(
        self, live
    ):
        """Signed in ON THE WAY to a consent page, no grant yet.

        This is the window between "Claude sent me here" and "I pressed
        Approve": there is no token to count, and the reader is still an
        OAuth reader. The signal is the session cookie's flow marker.
        """
        async with Session(live) as session:
            http = session.http
            client_id = await register(http)
            await sign_in(http, next_path=f"/connect/authorize?{authorize_query(client_id)}")
            page = await http.get("/connect")
            assert "Paste your key" in page.text  # no key yet: unchanged page
            saved = await http.post(
                "/connect/save",
                data={"csrf": csrf_of(page.text), "rapidapi_key": USER_KEY},
            )

        assert live.oauth_store._tokens == {}
        assert "You are set. There is nothing to paste." in saved.text
        assert CONNECT_TOKEN_PREFIX not in saved.text


class TestTokenReader:
    """Signed in, key stored, no client connected: the day-1 audience."""

    async def test_the_connect_url_is_there_but_not_on_screen(self, live):
        await store_key(live)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")

        assert "Your connect URL" in page.text
        assert "Paste this into your MCP client as the server URL" in page.text
        # The URL is in the document (the page exists to hand it over) but
        # inside a closed <details>, and the visible line carries no token.
        token = re.search(r"fp_token=(fpk_[^\s&<]+)", page.text).group(1)
        assert token.startswith(CONNECT_TOKEN_PREFIX)
        assert '<details class="reveal"' in page.text
        assert f'<pre id="fp-connect-url">{ORIGIN}/mcp?fp_token={token}</pre>' in page.text
        masked = re.search(r'<pre class="masked">([^<]*)</pre>', page.text).group(1)
        assert "fp_token=" in masked
        assert CONNECT_TOKEN_PREFIX not in masked

    async def test_the_masked_line_comes_before_the_reveal(self, live):
        await store_key(live)
        async with Session(live) as session:
            await sign_in(session.http)
            page = (await session.http.get("/connect")).text
        assert page.index('class="masked"') < page.index('id="fp-connect-url"')

    async def test_no_key_yet_is_the_paste_box_for_everyone(self, live):
        grant(live.oauth_store)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert "Paste your key" in page.text
        assert "at most one" in page.text
        assert CONNECT_TOKEN_PREFIX not in page.text


class TestRendererDirectly:
    """The last line of defence: the template itself, no server involved."""

    def _summary(self) -> KeySummary:
        return KeySummary(email=EMAIL, key_last4="9666", key_version=1)

    def test_the_oauth_variant_drops_a_token_it_is_handed(self):
        html = signed_in_html(
            email=EMAIL,
            product="flights",
            mcp_url=f"{ORIGIN}/mcp",
            summary=self._summary(),
            token="fpk_should-never-be-rendered",
            csrf="csrf",
            flow=FLOW_OAUTH,
        )
        assert "fpk_should-never-be-rendered" not in html
        assert "You are set" in html

    def test_the_token_variant_renders_it(self):
        html = signed_in_html(
            email=EMAIL,
            product="flights",
            mcp_url=f"{ORIGIN}/mcp",
            summary=self._summary(),
            token="fpk_real-token",
            csrf="csrf",
            flow=FLOW_TOKEN,
        )
        assert "fpk_real-token" in html
        assert '<details class="reveal"' in html

    def test_the_copy_button_starts_hidden(self):
        html = signed_in_html(
            email=EMAIL,
            product="flights",
            mcp_url=f"{ORIGIN}/mcp",
            summary=self._summary(),
            token="fpk_real-token",
            csrf="csrf",
            flow=FLOW_TOKEN,
        )
        assert 'id="fp-copy" hidden' in html


class TestStoreOutage:
    async def test_an_unreadable_oauth_store_falls_back_to_the_token_flow(
        self, live, monkeypatch
    ):
        """Fail towards the page that shipped, never towards hiding a URL."""

        async def boom(*args, **kwargs):
            raise RuntimeError("neon is having a moment")

        await store_key(live)
        grant(live.oauth_store)
        monkeypatch.setattr(live.oauth_store, "count_live_grants", boom)
        async with Session(live) as session:
            await sign_in(session.http)
            page = await session.http.get("/connect")
        assert page.status_code == 200
        assert CONNECT_TOKEN_PREFIX in page.text


# ── Deny ─────────────────────────────────────────────────────────────────


class TestLoopbackDetection:
    @pytest.mark.parametrize(
        "uri",
        [
            "http://127.0.0.1:33418/callback",
            "http://127.0.0.1/cb",
            "http://localhost:6274/oauth/callback",
            "http://[::1]:9999/cb",
            "http://0.0.0.0:8080/cb",
            "http://app.localhost:1234/cb",
            "http://127.5.5.5:1/cb",
        ],
    )
    def test_loopback(self, uri):
        assert is_loopback_redirect(uri) is True

    @pytest.mark.parametrize(
        "uri",
        [
            "https://client.example/callback",
            "https://cursor.com/oauth/cb",
            "cursor://anysphere.cursor-retrieval/oauth/callback",
            "http://10.0.0.4/cb",
            "",
            "not a url",
        ],
    )
    def test_not_loopback(self, uri):
        assert is_loopback_redirect(uri) is False


class TestDeny:
    async def _consent(self, http, redirect_uri):
        client_id = await register(http, redirect_uri)
        await sign_in(http)
        page = await http.get(
            f"/connect/authorize?{authorize_query(client_id, redirect_uri)}"
        )
        assert page.status_code == 200
        return page.text

    async def test_a_loopback_client_gets_our_page_not_chromes(self, live):
        await store_key(live)
        async with Session(live) as session:
            http = session.http
            page = await self._consent(http, LOOPBACK_REDIRECT)
            denied = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page, "csrf"),
                    "request": field(page, "request"),
                    "decision": "deny",
                },
            )

        assert denied.status_code == 200
        assert "location" not in denied.headers
        assert "You cancelled" in denied.text
        assert "Nothing was stored" in denied.text
        assert "127.0.0.1:33418" in denied.text
        # Nothing was issued, and the RFC answer is still one click away.
        assert live.oauth_store._codes == {}
        href = re.search(
            r'<a href="([^"]+)">Tell it you cancelled</a>', denied.text
        ).group(1)
        params = query_of(href.replace("&amp;", "&"))
        assert params["error"] == "access_denied"
        assert params["state"] == "state-123"

    async def test_a_hosted_client_is_still_redirected(self, live):
        """The RFC path is unchanged for a client that can show its own page."""
        await store_key(live)
        async with Session(live) as session:
            http = session.http
            page = await self._consent(http, HOSTED_REDIRECT)
            denied = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page, "csrf"),
                    "request": field(page, "request"),
                    "decision": "deny",
                },
            )

        assert denied.status_code == 303
        location = denied.headers["location"]
        assert location.startswith(HOSTED_REDIRECT)
        assert query_of(location)["error"] == "access_denied"

    async def test_a_rejected_request_to_a_loopback_client_gets_the_page_too(
        self, live
    ):
        """Same reasoning, other error: a bad `resource` on a native client."""
        async with Session(live) as session:
            http = session.http
            client_id = await register(http, LOOPBACK_REDIRECT)
            query = authorize_query(client_id).replace(
                urlencode({"resource": f"{ORIGIN}{MCP_OAUTH_PATH}"}),
                urlencode({"resource": "https://someone-else.test/mcp"}),
            )
            response = await http.get(f"/connect/authorize?{query}")

        assert response.status_code == 200
        assert "That sign-in could not be completed" in response.text
        href = re.search(
            r'<a href="([^"]+)">Tell it you cancelled</a>', response.text
        ).group(1)
        assert query_of(href.replace("&amp;", "&"))["error"] == "invalid_target"

    async def test_approve_is_untouched(self, live):
        """The change must not have moved the path that issues a code."""
        await store_key(live)
        async with Session(live) as session:
            http = session.http
            page = await self._consent(http, LOOPBACK_REDIRECT)
            approved = await http.post(
                "/connect/authorize",
                data={
                    "csrf": field(page, "csrf"),
                    "request": field(page, "request"),
                    "decision": "approve",
                },
            )

        assert approved.status_code == 303
        location = approved.headers["location"]
        assert location.startswith(LOOPBACK_REDIRECT)
        params = query_of(location)
        assert params["code"].startswith("fpc_")
        assert params["state"] == "state-123"


class TestGrantCounting:
    """`count_live_grants` is what the page hangs on. Pin its edges."""

    async def test_it_counts_only_live_rows_for_that_account(self):
        store = MemoryOAuthStore()
        now = time.time()
        grant(store)
        grant(store, token_hash="revoked", revoked_at=now)
        grant(store, token_hash="expired", expires_at=now - 1)
        grant(store, token_hash="other-user", user_sub="sub-2")
        grant(store, token_hash="other-provider", provider="github")

        assert await store.count_live_grants(SUB, "google") == 1
        assert await store.count_live_grants("sub-2", "google") == 1
        assert await store.count_live_grants("nobody", "google") == 0
