"""
The /connect page: sign in with Google, paste a RapidAPI key once, get a
connect URL back.

What the page is for
--------------------
Every other way of reaching this server makes the user handle the raw key in
their MCP client, and two of the three put it in a URL. /connect replaces
that with: sign in, paste the key into a real form over TLS, and copy back a
URL carrying a revocable `fpk_…` token. The key is encrypted before it is
stored and is never rendered again -- the page shows the last four characters
and nothing else, on every subsequent visit.

Validating the key
------------------
Storing a key that turns out to be wrong just moves the failure to the user's
first search, in their MCP client, with none of the context they had while
they were on this page. So the key is checked before it is stored, with ONE
request to the same RapidAPI listing the tools use:

    flights   POST https://google-flights-live-api.p.rapidapi.com
                   /api/google_flights/oneway/v1
    hotels    POST https://booking-live-api.p.rapidapi.com/search

with an empty JSON body. The body is empty deliberately: RapidAPI's gateway
answers the *authentication* question before our backend ever sees the
request, so 401 (bad key) and 403 (not subscribed to this API) come back in
milliseconds and cost the backend nothing. Anything else -- including the
400/422 our own backend returns for a body with no dates in it -- means the
gateway accepted the key, which is the whole question being asked.

Cost, stated plainly on the page: **at most one request against the user's
own plan.** RapidAPI counts a request once it proxies it, so a key that is
subscribed and in quota spends one; a key that is rejected at the gateway
spends none. On the free BASIC tier (10 requests a month) that is 10% of a
month's allowance, which is exactly why the page says so before the button
rather than after it.

`MCP_CONNECT_VALIDATE=0` turns the check off for a deployment that would
rather store first and fail later.

Two audiences, two pages
------------------------
Since day 2 there are two ways to reach the tools, and the people using them
arrive at /connect with different questions:

* **the OAuth flow** -- the user added `…/mcp/oauth` as a connector in
  Claude, Cursor or ChatGPT, signed in with Google and approved it. The
  client holds a token already. Their question is "did that work?", and the
  answer is one line: you are set, nothing to paste. Handing this user a
  `fp_token` URL is worse than useless -- the obvious next move is to paste
  it into the same client, which leaves them with two connectors to one
  server for one key.
* **the token flow** -- a client that cannot do OAuth. Their question is
  "what URL do I paste?", and the connect URL is the answer.

`signed_in_html` renders one or the other from `flow`; `src/server.py`
decides which, and documents how. The connect URL is never printed for the
OAuth reader, and even in the token flow it is collapsed behind a Reveal
control: it is a 90-day bearer credential for somebody's RapidAPI plan, and
a page that prints one by default leaks it to every screenshot, screen share
and person walking past.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .keystore import KeySummary
from .legal import CONTACT_EMAIL, page

logger = logging.getLogger(__name__)

#: Where the validation request goes, per product, and the listing a caller
#: has to be subscribed to for it to pass. Mirrors rapidapi_client.ENDPOINT_MAP
#: and hotels_client.ENDPOINT_MAP rather than re-deriving them, so a path that
#: moves there moves here in the same edit.
VALIDATION_TARGETS = {
    "flights": ("google-flights-live-api.p.rapidapi.com", "/api/google_flights/oneway/v1"),
    "hotels": ("booking-live-api.p.rapidapi.com", "/search"),
}
#: A combined deployment validates against flights: one key, one check, and
#: the flights listing is the primary one for `both`.
VALIDATION_TARGETS["both"] = VALIDATION_TARGETS["flights"]

LISTINGS = (
    (
        "Google Flights Live API",
        "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
    ),
    (
        "Booking Live API",
        "https://rapidapi.com/mtnrabi/api/booking-live-api",
    ),
)


@dataclass(frozen=True)
class KeyCheck:
    """The verdict on a pasted key. `ok=False` is always explainable."""

    ok: bool
    reason: str = ""
    message: str = ""
    quota: dict[str, int] | None = None


async def check_rapidapi_key(
    key: str,
    product: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 15.0,
) -> KeyCheck:
    """One gateway round trip. Never raises: a network failure is reported as
    `unverified`, which the caller stores anyway rather than losing a key
    because our own egress hiccuped."""
    from .rapidapi_client import read_quota  # local: keeps the import graph flat

    host, path = VALIDATION_TARGETS.get(product, VALIDATION_TARGETS["flights"])
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            f"https://{host}{path}",
            json={},
            headers={
                "content-type": "application/json",
                "x-rapidapi-key": key,
                "x-rapidapi-host": host,
            },
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("key validation could not reach RapidAPI: %s", type(exc).__name__)
        return KeyCheck(
            ok=True,
            reason="unverified",
            message=(
                "We could not reach RapidAPI to check the key just now, so it "
                "was saved without being verified. If searches come back "
                "asking for a key, come back here and paste it again."
            ),
        )
    finally:
        if own_client:
            await client.aclose()

    quota = read_quota(response)
    if response.status_code == 401:
        return KeyCheck(
            ok=False,
            reason="invalid",
            message="RapidAPI did not recognise that key. Copy it again from the listing page.",
        )
    if response.status_code == 403:
        return KeyCheck(
            ok=False,
            reason="not_subscribed",
            message=(
                "That key is valid but is not subscribed to this API. Open the "
                "listing below and subscribe -- the BASIC plan is free."
            ),
        )
    if response.status_code == 429:
        return KeyCheck(
            ok=True,
            reason="out_of_quota",
            message=(
                "The key works, but this month's plan quota is already spent. "
                "It is saved; searches will start working again when the plan "
                "resets or you upgrade."
            ),
            quota=quota,
        )
    return KeyCheck(ok=True, reason="ok", quota=quota)


# ── rendering ────────────────────────────────────────────────────────────

_EXTRA_STYLE = """
<style>
.card { border: 1px solid rgba(128,128,128,.35); border-radius: 8px;
        padding: 1.25rem 1.25rem .25rem; margin: 1.5rem 0; }
.btn { display: inline-block; border: 1px solid rgba(128,128,128,.55);
       border-radius: 6px; padding: .55rem 1rem; text-decoration: none;
       font-weight: 600; background: rgba(128,128,128,.12); cursor: pointer;
       font-size: 1rem; color: inherit; }
.btn.danger { font-weight: 400; }
input[type=text], input[type=password] {
  width: 100%; box-sizing: border-box; padding: .55rem .6rem; font-size: 1rem;
  border: 1px solid rgba(128,128,128,.5); border-radius: 6px;
  background: transparent; color: inherit;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.note { font-size: .9rem; opacity: .8; }
.bad { border-left: 3px solid #c0392b; padding-left: .75rem; }
.good { border-left: 3px solid #27865a; padding-left: .75rem; }
pre { background: rgba(128,128,128,.12); padding: .8rem; border-radius: 6px;
      overflow-x: auto; font-size: .9rem; }
details.reveal > summary { cursor: pointer; font-weight: 600;
      display: inline-block; border: 1px solid rgba(128,128,128,.55);
      border-radius: 6px; padding: .45rem .9rem;
      background: rgba(128,128,128,.12); list-style: none; }
details.reveal > summary::-webkit-details-marker { display: none; }
details.reveal { margin: .6rem 0; }
.masked { user-select: none; opacity: .75; }
</style>
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _listings_html() -> str:
    rows = "".join(
        f'<li><a href="{_e(url)}">{_e(name)}</a></li>' for name, url in LISTINGS
    )
    return (
        "<p>Get a key by subscribing to the listing you want to use. "
        "<strong>BASIC is free</strong> and includes 10 requests a month; "
        "PRO is $10 a month.</p>"
        f"<ul>{rows}</ul>"
    )


def signed_out_html(product: str, banner: str = "") -> str:
    warning = f'<p class="bad">{_e(banner)}</p>' if banner else ""
    return page(
        "Connect your RapidAPI key",
        _EXTRA_STYLE
        + "<h1>Connect your RapidAPI key</h1>"
        + warning
        + "<p>Sign in with Google, paste your RapidAPI key once, and this "
        "server will hand you a connect URL to put in your MCP client. The "
        "key itself never goes in that URL, and you can disconnect it here "
        "at any time.</p>"
        '<p><a class="btn" href="/connect/start">Sign in with Google</a></p>'
        '<p class="note">We ask Google for two things only: your account id '
        "and your email address. Nothing else is requested and nothing else "
        "is stored. Your RapidAPI key is encrypted before it is written down "
        "and is never shown again -- only its last four characters.</p>"
        "<h2>Where the key comes from</h2>" + _listings_html()
        + f'<p class="note">Questions: {_e(CONTACT_EMAIL)}. '
        '<a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>',
    )


#: Which page a signed-in visitor gets. Decided in `src/server.py`; passed in
#: rather than worked out here so the rendering stays a pure function.
FLOW_OAUTH = "oauth"
FLOW_TOKEN = "token"

#: Where a signed-in OAuth user goes when their OTHER client cannot sign in.
#: One link, so the connect URL is reachable without being printed at anyone.
TOKEN_FLOW_PATH = "/connect?token=1"

#: Reveal + Copy. Twelve lines rather than a dependency, and the page works
#: with the script blocked: the <details> element opens on its own, and the
#: Copy button starts hidden and is only shown by this script, so nobody is
#: offered a button that cannot work.
_REVEAL_SCRIPT = """
<script>
(function () {
  var url = document.getElementById("fp-connect-url");
  var copy = document.getElementById("fp-copy");
  if (!url || !copy || !navigator.clipboard) { return; }
  copy.hidden = false;
  copy.addEventListener("click", function () {
    navigator.clipboard.writeText(url.textContent.trim()).then(function () {
      copy.textContent = "Copied";
      setTimeout(function () { copy.textContent = "Copy"; }, 2000);
    });
  });
})();
</script>
"""


#: The stand-in for the token in the collapsed view. Deliberately NOT the
#: first few characters of the real one: a placeholder that starts with
#: `fpk_` is a placeholder that gets copied, pasted and reported as broken.
_MASK = "•" * 16


def connect_url_of(mcp_url: str, token: str) -> str:
    joiner = "&" if "?" in mcp_url else "?"
    return f"{mcp_url}{joiner}fp_token={token}"


def _connect_url_block(mcp_url: str, token: str) -> str:
    """The token flow's answer: a URL to paste, hidden until asked for.

    The URL is in the HTML -- it has to be, this page exists to hand it over
    -- but it is inside a closed <details>, so it is not on screen, not in a
    screenshot and not in a screen share unless the user opens it. That is
    the whole of the protection being claimed here, and it is the right size
    for the risk: the reader is alone on their own machine, and the thing
    that leaked it in practice was a shared screen.
    """
    full = connect_url_of(mcp_url, token)
    masked = connect_url_of(mcp_url, _MASK)
    return (
        "<h2>Your connect URL</h2>"
        "<p>Paste this into your MCP client as the server URL. It carries a "
        "token, not your key.</p>"
        f'<pre class="masked">{_e(masked)}</pre>'
        '<details class="reveal"><summary>Reveal the URL</summary>'
        f'<pre id="fp-connect-url">{_e(full)}</pre></details>'
        '<p><button class="btn" type="button" id="fp-copy" hidden>Copy</button></p>'
        '<p class="note">Hidden on purpose. Anyone who reads this URL can '
        "spend your RapidAPI plan until you disconnect, so keep it out of "
        "screenshots and screen shares.</p>"
        "<p>Clients that let you set a header can send the same token as "
        f"<code>Authorization: Bearer {_e(token[:8])}…</code> instead, which "
        "keeps it out of the URL.</p>"
        '<p class="note">The token is valid for 90 days and stops working the '
        "moment you disconnect below. It is not your RapidAPI key and cannot "
        "be turned back into it.</p>" + _REVEAL_SCRIPT
    )


def signed_in_html(
    *,
    email: str,
    product: str,
    mcp_url: str,
    summary: KeySummary | None,
    token: str | None,
    csrf: str,
    notice: str = "",
    error: str = "",
    flow: str = FLOW_TOKEN,
) -> str:
    """The page for a signed-in visitor.

    `flow` picks which of the two questions in the module docstring is being
    answered. `token` is only ever rendered in `FLOW_TOKEN`, and the caller
    is expected not to mint one at all for the other flow -- both halves of
    that are asserted in tests, because "we pass None there" is exactly the
    kind of caller-side promise that a later refactor breaks quietly.
    """
    blocks = [_EXTRA_STYLE, "<h1>Connect your RapidAPI key</h1>"]
    blocks.append(
        f'<p class="note">Signed in as <strong>{_e(email or "your Google account")}'
        "</strong>.</p>"
    )
    if error:
        blocks.append(f'<p class="bad">{_e(error)}</p>')
    if notice:
        blocks.append(f'<p class="good">{_e(notice)}</p>')

    if summary is not None:
        oauth_reader = flow == FLOW_OAUTH
        connected = (
            '<div class="card"><h2>Connected</h2>'
            f"<p>RapidAPI key ending <code>…{_e(summary.key_last4)}</code>.</p>"
        )
        if oauth_reader:
            # The whole point of this branch: someone whose assistant is
            # already connected needs one sentence telling them to go back to
            # it, not a URL to paste into a second connector.
            connected += (
                "<p><strong>You are set. There is nothing to paste.</strong> "
                "Go back to your assistant and ask it for a fare.</p>"
                '<p class="note">Searches it runs are billed to your own '
                "RapidAPI plan, and stop the moment you disconnect below.</p>"
            )
        connected += "</div>"
        blocks.append(connected)
        if oauth_reader:
            blocks.append(
                '<p class="note">Using another client that cannot sign in? '
                f'<a href="{_e(TOKEN_FLOW_PATH)}">Get a connect URL</a> for '
                "it instead.</p>"
            )
        elif token:
            blocks.append(_connect_url_block(mcp_url, token))
        blocks.append(
            "<h2>Replace it</h2>"
            "<p>Paste a different key to overwrite the stored one. "
            + (
                "Your connected clients keep working and start using the new "
                "key straight away.</p>"
                if oauth_reader
                else "The connect URL above keeps working.</p>"
            )
        )
    else:
        blocks.append("<h2>Paste your key</h2>")
        blocks.append(_listings_html())

    blocks.append(
        '<form method="post" action="/connect/save"><div class="card">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        '<p><label for="rapidapi_key">RapidAPI key</label></p>'
        '<p><input id="rapidapi_key" name="rapidapi_key" type="password" '
        'autocomplete="off" spellcheck="false" '
        'placeholder="the 50-character key from your RapidAPI dashboard"></p>'
        '<p><button class="btn" type="submit">Save key</button></p>'
        '<p class="note">Saving runs one check against RapidAPI to make sure '
        "the key is subscribed. That check costs <strong>at most one "
        "request</strong> from your own plan -- on the free BASIC plan, one "
        "of ten for the month.</p>"
        "</div></form>"
    )

    if summary is not None:
        blocks.append(
            '<form method="post" action="/connect/disconnect">'
            f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
            '<p><button class="btn danger" type="submit">Disconnect</button></p>'
            '<p class="note">Disconnecting deletes the stored key, '
            + (
                "signs out every assistant you connected"
                if flow == FLOW_OAUTH
                else "makes every connect URL for this account stop resolving"
            )
            + ", and takes effect immediately. It does not "
            "touch your RapidAPI account or your subscription -- only the copy "
            "we hold.</p></form>"
        )

    blocks.append(
        f'<hr><p class="note">Questions: {_e(CONTACT_EMAIL)}. '
        '<a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>'
    )
    return page("Connect your RapidAPI key", "".join(blocks))


# ── wiring ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectSupport:
    """Everything the /connect routes and the stored-key lookup need.

    Built once per product in `build_server`, or not at all. `None` is the
    normal state: a deployment without GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET, MCP_KEY_MASTER and DATABASE_URL registers no
    routes and changes no request path, which is what makes this feature a
    no-op on the two live deployments until ops sets all four.
    """

    store: Any          # keystore.KeyStore
    auth: Any           # webauth.GoogleWebAuth
    product: str
    mcp_url: str
    validate: bool = True

    def csrf(self, sub: str) -> str:
        """A per-session form token.

        The session cookie is SameSite=Lax, which already stops a
        cross-site POST from carrying it. This is the second lock, and it
        costs one hmac: a hidden field bound to the signed-in account, so a
        form replayed for a different account is rejected on its value rather
        than on its cookie.
        """
        import hashlib as _hashlib
        import hmac as _hmac

        return _hmac.new(
            self.auth.session_secret, f"csrf:{sub}".encode(), _hashlib.sha256
        ).hexdigest()[:32]

    def csrf_ok(self, sub: str, given: str) -> bool:
        import hmac as _hmac

        return bool(given) and _hmac.compare_digest(self.csrf(sub), given)


def build_connect_support(
    product: str, origin: str, mcp_url: str
) -> "ConnectSupport | None":
    """The /connect feature for one product, or None when unconfigured.

    Never raises. A half-configured deployment (a database but no Google
    client, say) is a deployment where the page would not work, and the right
    behaviour there is the behaviour of every deployment today: serve keyed
    callers and register nothing.
    """
    import os as _os

    from .keystore import MasterKeyError, build_key_store, load_master_key
    from .webauth import build_web_auth

    store = build_key_store()
    if not getattr(store, "available", False):
        return None
    try:
        master = load_master_key()
    except MasterKeyError:
        # build_key_store already logged the reason and returned a
        # NullKeyStore, so this branch is unreachable in practice; kept
        # because "unreachable" is a claim about two functions staying in
        # step, and this one is cheap.
        return None
    auth = build_web_auth(origin, master)
    if auth is None:
        logger.info(
            "key store is configured but GOOGLE_OAUTH_CLIENT_ID/SECRET are "
            "not; /connect stays unregistered."
        )
        return None
    return ConnectSupport(
        store=store,
        auth=auth,
        product=product,
        mcp_url=mcp_url,
        validate=_os.environ.get("MCP_CONNECT_VALIDATE", "1").strip().lower()
        not in {"0", "false", "no"},
    )
