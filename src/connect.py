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
unmetered GET to a path that is configured on no listing:

    flights   GET https://google-flights-live-api.p.rapidapi.com/fp-subscription-check
    hotels    GET https://booking-live-api.p.rapidapi.com/fp-subscription-check

RapidAPI checks the key and the subscription BEFORE it routes, so the answer
comes from the Hub itself and nothing reaches our backend (#523, measured
2026-09-28 for the api front's `/v1/verify`; the two answers below were
re-measured 2026-10-23 on both hosts):

    a key subscribed to that listing        404 "Endpoint '/...' does not exist"
    an unknown key, or one not subscribed   403 "You are not subscribed to this API."

The Hub's 404 is not counted against the plan (#523: three probes between two
metered calls moved `x-ratelimit-requests-remaining` by the metered call
only). Until 2026-10-23 this check was a
POST with an empty body to the real search endpoint: one metered request,
and our backend's 422 for the missing dates landed in the customer's
RapidAPI analytics as their first (and failed) call.

The Hub sends the same 403 for a wrong key and for an unsubscribed one, so
the page's message names both fixes. Fail-open, as before: a network failure,
or any answer that is neither the Hub's 404 nor a 401/403 (a 429 included),
stores the key as `unverified` rather than losing it; only 401/403 refuse it. The path must
never be configured as an endpoint on a listing; if it ever is, a 2xx still
verifies, it just starts costing a request.

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
from .quota_gate import BASIC_FOOTNOTE, pricing_url, pro_line

logger = logging.getLogger(__name__)

#: Which listing host the key is checked against, per product. A combined
#: deployment validates against flights: one key, one check, and the flights
#: listing is the primary one for `both`.
VALIDATION_HOSTS = {
    "flights": "google-flights-live-api.p.rapidapi.com",
    "hotels": "booking-live-api.p.rapidapi.com",
}
VALIDATION_HOSTS["both"] = VALIDATION_HOSTS["flights"]
#: Same path as the api front's `/v1/verify` (api_proxy SUBSCRIPTION_PROBE_PATH).
SUBSCRIPTION_PROBE_PATH = "/fp-subscription-check"


def is_hub_no_such_endpoint(response: httpx.Response) -> bool:
    """The Hub's own 404 for an unconfigured path, sent only after it accepted
    the key for the listing. Matched on the message: a backend's 404 says
    nothing about whether the Hub looked at the key."""
    if response.status_code != 404:
        return False
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        return False
    return (
        isinstance(message, str)
        and message.startswith("Endpoint ")
        and message.endswith("does not exist")
    )


_UNVERIFIED = (
    "We could not reach RapidAPI to check the key just now, so it "
    "was saved without being verified. If searches come back "
    "asking for a key, come back here and paste it again."
)


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
    """One unmetered Hub round trip. Never raises: a network failure is
    reported as `unverified`, which the caller stores anyway rather than
    losing a key because our own egress hiccuped."""
    from .rapidapi_client import read_quota  # local: keeps the import graph flat

    host = VALIDATION_HOSTS.get(product, VALIDATION_HOSTS["flights"])
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.get(
            f"https://{host}{SUBSCRIPTION_PROBE_PATH}",
            headers={"x-rapidapi-key": key, "x-rapidapi-host": host},
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("key validation could not reach RapidAPI: %s", type(exc).__name__)
        return KeyCheck(ok=True, reason="unverified", message=_UNVERIFIED)
    finally:
        if own_client:
            await client.aclose()

    quota = read_quota(response)
    if is_hub_no_such_endpoint(response) or 200 <= response.status_code < 300:
        return KeyCheck(ok=True, reason="ok", quota=quota)
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
                "RapidAPI did not accept that key for this API: it is either "
                "not subscribed to it, or not copied whole. Open the listing "
                "below and subscribe -- the BASIC plan is free -- then copy "
                "the key again from that page."
            ),
        )
    # Anything else, 429 included, is not an answer about the key: a 429 on
    # this unmetered path was never measured as "quota spent" (a random key
    # got one on the old POST path), so it must not say "the key works".
    logger.warning("key validation got an unexpected %s from RapidAPI", response.status_code)
    return KeyCheck(
        ok=True,
        reason="unverified",
        message=(
            "RapidAPI gave an unexpected answer when we checked the key, so "
            "it was saved without being verified. If searches come back "
            "asking for a key, come back here and paste it again."
        ),
    )


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
    # PRO leads: one month-wide scan is 93 searches, so the 10-a-month BASIC
    # plan is a footnote, never the offer (2026-10-22).
    rows = "".join(
        f'<li><a href="{_e(pricing_url(url))}">{_e(name)}</a>: '
        f"<strong>{_e(pro_line(url))}</strong></li>"
        for name, url in LISTINGS
    )
    return (
        "<p>Get a key by subscribing to the listing you want to use.</p>"
        f"<ul>{rows}</ul>"
        f'<p class="note">{_e(BASIC_FOOTNOTE)}</p>'
    )


#: What to type first, per product: one real question a new user can paste.
#: The month-wide flights ask is ~62 searches, so it is offered to a caller
#: with a key; a caller on the small daily sign-in allowance gets a one-search
#: question instead, or their first try would be a quota refusal.
FIRST_ASK = {
    "flights": (
        "cheapest round trip from <your city> to Lisbon any week in March, "
        "7 or 10 nights"
    ),
    "flights_small": "cheapest round trip from <your city> to Lisbon, 10 to 17 March",
    "hotels": "price The Hoxton, Amsterdam for 10 to 14 March for 2 adults",
}


#: The redirect URIs claude.ai's own connector client registers with (the
#: web app; Claude Desktop's connectors come from the same account and the
#: same callback). Only a client sent back to one of these gets Claude's menu
#: path. Matched on the redirect URI, not on the client's NAME: Claude Code
#: and any other tool can put "Claude" in a client_name, and Claude Code adds
#: a server with `/mcp`, not "+ -> Connectors". `claude.com` is listed
#: because Anthropic's connector docs say the callback may move there.
CLAUDE_AI_REDIRECT_URIS = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
)


def is_claude_ai_client(redirect_uri: str) -> bool:
    """Whether this OAuth client is claude.ai's connector client."""
    return (redirect_uri or "").strip() in CLAUDE_AI_REDIRECT_URIS


def next_step_text(
    product: str,
    lead: str = "You're connected. In a new chat:",
    *,
    small: bool = False,
    claude_ai: bool = False,
) -> str:
    """The one-paragraph "you're connected, now do this" a new user needs.

    Names the client path, one question to paste, and the ready-made prompts
    this host ships, so the first search is one step away. The "+ ->
    Connectors" path is Claude's web/desktop menu and is only named when
    `claude_ai` says the client IS claude.ai (see `is_claude_ai_client`);
    every other client, and every page that cannot tell which client it is
    serving (/connect), gets a neutral path. `small` picks a one-search
    question for a caller on the daily allowance.
    """
    from .prompts import FLIGHT_PROMPT_TITLES, HOTEL_PROMPT_TITLES  # noqa: PLC0415

    hotels = product == "hotels"
    if hotels:
        ask = FIRST_ASK["hotels"]
    else:
        ask = FIRST_ASK["flights_small" if small else "flights"]
    titles = HOTEL_PROMPT_TITLES if hotels else FLIGHT_PROMPT_TITLES
    path = (
        "+ \u2192 Connectors \u2192 turn FlightPowers on"
        if claude_ai
        else "make sure FlightPowers is turned on in your assistant's "
        "connectors or tools menu"
    )
    first = f"{lead} {path}, then ask: '{ask}'."
    if small:
        # The ready-made prompts ask for date ranges, which need more
        # searches than the daily allowance has: offering them to a caller
        # on it sends their first try into a refusal (review 2026-10-22).
        return first
    return (
        f"{first} Or pick one of its ready-made prompts: "
        + ", ".join(f"'{t}'" for t in titles)
        + "."
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
    trial_line: str = "",
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
    if trial_line:
        # The keyless allowance, in one sentence. Rendered high on the page on
        # purpose: a signed-in visitor who has not pasted a key is already
        # able to search, and a page that only ever says "paste a key" tells
        # them the opposite of what is true.
        blocks.append(f'<p class="note">{_e(trial_line)}</p>')
        if flow == FLOW_OAUTH and summary is None:
            # Signed in through an assistant, no key yet, and the allowance
            # is on: they can search right now, so say how.
            blocks.append(
                f'<p class="good">{_e(next_step_text(product, small=True))}</p>'
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
                "<p><strong>You are set. There is nothing to paste.</strong></p>"
                f"<p>{_e(next_step_text(product))}</p>"
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
        "the key is subscribed. That check costs <strong>no requests"
        "</strong> from your plan.</p>"
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
