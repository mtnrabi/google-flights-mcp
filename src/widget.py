"""The flights result card: one self-contained MCP Apps widget, no ads.

What this is
------------
A host that renders MCP UI (claude.ai, ChatGPT) asks the server for an
HTML resource and hands the tool's `structuredContent` to it over a
postMessage bridge. This module is that resource for the two flights
tools: a compact fare table, Google's own price band drawn as a
three-segment bar with the verdict and the cheapest fare marked on it,
and a Book button per row.

It shows the ten cheapest rows and keeps the rest one scroll away inside
the table (with a "Show all N" button for a host that swallows the inner
scroll), because a thirty-fare answer rendered as a thirty-row card is a
wall in a chat window. The height ceiling applies whatever the row count
is, and gives way to a floor of five rows: on a 380px frame a row is a
184px block, and a card that answers a 36-fare search with two fares is
not an answer. Below 520px the band collapses to one line so the height
it costs goes to fares instead.

Destinations: both tools take a LIST of destination airports and answer
with one flat `results` array, so "TLV to Rome, Athens or Budapest"
arrives as thirty rows with the Athens fares wherever they happen to sit.
Rows are grouped on the RAW `to_airport` value -- two different strings
are two destinations, and guessing they are the same would merge two
routes' fares -- and only the LABEL is shortened to the code. A row with
no `to_airport` goes in an "Other" bucket with its own pill rather than
being filtered out of every view of the card: it is still a fare the
caller paid for. One bucket means no pills; a filter row with a single
button is a control that cannot do anything. When the call asked for several destinations --
both tools take a LIST -- the destinations become filter pills above the
table, "All" selected by default; picking one filters the rows in the
frame and recomputes the band and the cheapest marker for that
destination alone. While several routes are in the table each row also
carries its destination code, and the band says which route it belongs
to -- Google tracks each route separately, so the band drawn is the one
belonging to the route that holds the cheapest fare.

What this is NOT
----------------
The free (Lulu) server's widget, which comes out of the `lulu_ads` SDK
and carries a sponsored strip and an impression beacon. **This server
carries no ads, ever** -- ad-carrying servers can never be listed
(Anthropic Directory Policy 4.C, OpenAI plugin guidelines, the official
MCP registry ToS), and both paid listings are directory-listed. So the
frame below is hand-rolled: no SDK, no beacon, no third-party origin, and
the widget makes NO network request of any kind. Everything it draws, it
draws from the tool result the host already delivered.

Nothing changes for a host that does not render UI
--------------------------------------------------
This adds a resource and a `_meta` key on two tool definitions. The tool
RESULTS are byte-for-byte what they were: no field is added to the
payload, no output schema moves. Cursor, Claude Code, curl, Smithery and
every script see exactly today's JSON and today's text block. That is why
the widget computes the eyebrow, the currency symbol, the cheapest fare
and the Book label in JavaScript instead of having the server inject them
as extra fields the way the free server does.

The domain hash (read this before touching `WIDGET_URI` or the CSP)
-------------------------------------------------------------------
claude.ai validates the widget's `_meta.ui.domain` against
``sha256(<the connector URL the user added>)[:32] + ".claudemcpcontent.com"``
and silently refuses the frame on a mismatch -- the user gets a red
"Unable to reach FlightPowers" chip next to a perfectly good answer. On
2026-09-15 that cost the free server every rendered impression it had
ever served, because `MCP_PUBLIC_URL` was the bare origin and the hash
was one path segment out.

This deployment serves BOTH paid hostnames
(`google-flights-mcp.flightpowers.com` and `flights.flightpowers.com`,
plus `hotels.flightpowers.com` which gets no flights tools at all) from
one process, so a single baked-in domain would be right for exactly one
of them. `src/widget_domain.py` rewrites it per request from the Host the
caller actually connected to, the same way the OAuth issuer is chosen.
`claude_apps_domain` and `canonical_connector_url` live here so that the
hash and the URL it is taken of are defined in one place.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

#: The resource the flights tools point at. A `ui://` URI is the MCP Apps
#: convention; the host never fetches it over HTTP, it reads it through
#: `resources/read` on the same session.
WIDGET_URI = "ui://flightpowers/flights-card.html"

#: `text/html;profile=mcp-app` is what marks a resource as an app frame
#: rather than a document. fastmcp exports the same string as
#: `fastmcp.utilities.mime.UI_MIME_TYPE`; it is written out here so this
#: module has no import-time dependency on a private-ish path.
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"

#: Where clients connect. Mirrors `oauth.MCP_PATH`; "Single `/mcp` on both
#: MCP servers" is the standing decision, so this is the path every printed
#: guide, listing and reply names.
MCP_MOUNT_PATH = "/mcp"

#: The suffix every MCP Apps content domain carries. A `ui.domain` that does
#: not end in it was not produced by `claude_apps_domain`.
APPS_DOMAIN_SUFFIX = ".claudemcpcontent.com"

#: ASGI scope key carrying the path the CLIENT actually requested, stashed
#: by `oauth.OAuthResourceGate` before it rewrites `/mcp/oauth` to `/mcp`.
#:
#: The gate has to rewrite, because FastMCP mounts a plain `Route("/mcp")`
#: and `/mcp/oauth` would otherwise reach nothing. But by the time this
#: module runs, `request.url.path` says `/mcp` for a caller who connected to
#: `/mcp/oauth` -- and claude.ai hashes the URL the USER typed. Without the
#: stash, every connector added on the `/mcp/oauth` alias gets a domain that
#: is one path segment out and the card silently never renders: exactly the
#: 2026-09-15 failure, for the population that followed the printed guide.
#:
#: Declared here, in a module that imports nothing but the standard library,
#: so the gate and the hash agree on the key without an import cycle.
ORIGINAL_PATH_SCOPE_KEY = "fp_original_path"


def claude_apps_domain(connector_url: str) -> str:
    """The exact `_meta.ui.domain` claude.ai expects for `connector_url`.

    Deterministic and unregistered -- the host recomputes the same hash
    from the URL the user added and compares. Same function as
    `lulu_ads.widget.claude_apps_domain`, reimplemented in three lines
    because this server does not (and must not) depend on the ads SDK.
    """
    digest = hashlib.sha256(connector_url.encode()).hexdigest()[:32]
    return f"{digest}{APPS_DOMAIN_SUFFIX}"


def canonical_connector_url(raw: str) -> str:
    """`raw` in the shape a client actually connects to.

    A bare origin gets `/mcp` appended and a trailing slash is dropped,
    because both hash differently from the URL in the docs and a
    mismatched hash is invisible: the card simply never renders. This is
    the 2026-09-15 incident, applied here before it can happen again.
    Anything that is not a URL is handed back untouched rather than
    guessed at.
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        return value
    parts = urlsplit(value)
    if not parts.netloc:
        return value
    if parts.path in ("", "/"):
        return urlunsplit(
            (parts.scheme, parts.netloc, MCP_MOUNT_PATH, parts.query, parts.fragment)
        )
    return value


# ── the frame ────────────────────────────────────────────────────────────
#
# One document: markup, CSS and JS inline, no external font, no image, no
# fetch. The host applies its own sandbox CSP to the iframe and the default
# is restrictive; a widget that needed an external origin would have to
# declare it and would then be one blocked request away from a hole in the
# card. This one needs none.
#
# Every runtime value is written with `textContent` (never `innerHTML`), so
# a hostile upstream string cannot break out of the markup, and only
# http/https URLs are ever handed to the host's link opener.
FLIGHTS_WIDGET_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlightPowers fares</title>
<style>
  /* The host paints its own ground behind the frame; `color-scheme` is what
     stops Chromium putting an opaque white sheet under a dark host. */
  html, body { background: transparent; color-scheme: light dark; margin: 0; }
  * { box-sizing: border-box; }

  /* Light is the base; dark is redefined twice -- once for the host that
     tells us nothing (prefers-color-scheme) and once for the host that
     hands us a theme over ui/initialize (data-theme). A colour defined
     only inside a media query is a colour the toggle cannot reach. */
  :root {
    --fp-bg: #ffffff;
    --fp-ink: #16181d;
    --fp-soft: #5c6470;
    --fp-faint: #8b93a1;
    --fp-line: #e4e7ec;
    --fp-line-soft: #f0f2f5;
    --fp-accent: #1f6feb;
    --fp-accent-ink: #ffffff;
    --fp-low: #1f9d55;
    --fp-typical: #d99516;
    --fp-high: #d1493f;
    --fp-track: #eceff3;
    --fp-warn-bg: #fff6e6;
    --fp-warn-ink: #7a5306;
    --fp-radius: 14px;
    --fp-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --fp-bg: #1b1d21;
      --fp-ink: #f2f4f7;
      --fp-soft: #a9b1bd;
      --fp-faint: #7d8694;
      --fp-line: #31353c;
      --fp-line-soft: #26292e;
      --fp-accent: #5ea0ff;
      --fp-accent-ink: #10131a;
      --fp-low: #48c98a;
      --fp-typical: #e5b45c;
      --fp-high: #f0746a;
      --fp-track: #2a2e34;
      --fp-warn-bg: #3a2f14;
      --fp-warn-ink: #f0d79a;
    }
  }
  :root[data-theme="dark"] {
    --fp-bg: #1b1d21;
    --fp-ink: #f2f4f7;
    --fp-soft: #a9b1bd;
    --fp-faint: #7d8694;
    --fp-line: #31353c;
    --fp-line-soft: #26292e;
    --fp-accent: #5ea0ff;
    --fp-accent-ink: #10131a;
    --fp-low: #48c98a;
    --fp-typical: #e5b45c;
    --fp-high: #f0746a;
    --fp-track: #2a2e34;
    --fp-warn-bg: #3a2f14;
    --fp-warn-ink: #f0d79a;
  }

  body {
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    color: var(--fp-ink);
  }
  .fp-card {
    background: var(--fp-bg);
    border: 1px solid var(--fp-line);
    border-radius: var(--fp-radius);
    overflow: hidden;
  }
  .fp-hd { padding: 14px 16px 10px; }
  .fp-route { font-size: 15px; font-weight: 650; letter-spacing: -.01em; }
  .fp-sub { font-size: 12px; color: var(--fp-soft); margin-top: 2px; }

  /* ── the price band ── */
  .fp-band { padding: 2px 16px 12px; }
  .fp-band-top {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 8px; font-size: 12px; color: var(--fp-soft); margin-bottom: 6px;
  }
  .fp-verdict {
    font-weight: 700; text-transform: uppercase; letter-spacing: .08em;
    font-size: 11px;
  }
  .fp-verdict[data-v="low"] { color: var(--fp-low); }
  .fp-verdict[data-v="typical"] { color: var(--fp-typical); }
  .fp-verdict[data-v="high"] { color: var(--fp-high); }
  .fp-bar {
    position: relative; height: 8px; border-radius: 999px;
    background: var(--fp-track); display: flex; overflow: visible;
  }
  .fp-seg { height: 8px; }
  .fp-seg:first-child { border-radius: 999px 0 0 999px; }
  .fp-seg:last-child { border-radius: 0 999px 999px 0; }
  .fp-seg[data-s="low"] { background: var(--fp-low); }
  .fp-seg[data-s="typical"] { background: var(--fp-typical); }
  .fp-seg[data-s="high"] { background: var(--fp-high); }
  .fp-mark {
    position: absolute; top: -4px; width: 4px; height: 16px; border-radius: 2px;
    background: var(--fp-ink); box-shadow: 0 0 0 2px var(--fp-bg);
    transform: translateX(-2px);
  }
  .fp-scale {
    display: flex; justify-content: space-between;
    font-size: 11px; color: var(--fp-faint); margin-top: 6px;
    font-family: var(--fp-mono);
  }

  /* ── destination pills ──
     A call that asked for four cities comes back as one table. The pills
     are how you get to one city's fares without reading the other three. */
  .fp-pills { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 12px; }
  .fp-pill {
    font: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
    padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--fp-line); background: transparent; color: var(--fp-soft);
  }
  .fp-pill:hover { border-color: var(--fp-accent); color: var(--fp-ink); }
  .fp-pill:focus-visible { outline: 2px solid var(--fp-ink); outline-offset: 2px; }
  .fp-pill[aria-pressed="true"] {
    background: var(--fp-accent); border-color: var(--fp-accent);
    color: var(--fp-accent-ink);
  }
  .fp-pill-n { opacity: .72; font-weight: 500; margin-left: 5px; }
  /* Only rendered while the table shows more than one route. */
  .fp-t td.fp-dest { font-weight: 700; letter-spacing: .02em; white-space: nowrap; }

  /* ── the table ── */
  .fp-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  /* Set only once the rows are capped, so a short table is never a
     scroll container that swallows the page's own wheel events. */
  .fp-scroll[data-capped="1"] { overflow-y: auto; }
  table.fp-t { width: 100%; border-collapse: collapse; font-size: 13px; }
  .fp-t th {
    text-align: left; font-size: 10px; font-weight: 700; letter-spacing: .09em;
    text-transform: uppercase; color: var(--fp-faint);
    padding: 6px 10px; border-top: 1px solid var(--fp-line);
    border-bottom: 1px solid var(--fp-line); white-space: nowrap;
    /* Sticky, so scrolling row 11 into view does not lose the column
       names. `border-collapse: collapse` drops the borders off a sticky
       cell in Chromium, so they are redrawn as inset shadows. */
    position: sticky; top: 0; z-index: 1; background: var(--fp-bg);
    box-shadow: inset 0 1px 0 var(--fp-line), inset 0 -1px 0 var(--fp-line);
  }
  .fp-t td {
    padding: 7px 10px; border-bottom: 1px solid var(--fp-line-soft);
    vertical-align: top;
  }
  /* Where there is room the time cells do not wrap: wrapped, a row is
     90px instead of 51, and ten of those is the wall this version exists
     to remove. Measured at 700, not 640, because the multi-destination
     table has a SEVENTH column and at 640 it pushed the row 17px
     sideways. Below it they wrap (taller rows, fewer visible, and the
     footer says so) rather than scrolling sideways. */
  @media (min-width: 700px) {
    .fp-t td.fp-when, .fp-t td.fp-when .fp-leg2,
    .fp-t td.fp-dur { white-space: nowrap; }
  }
  .fp-t tr:last-child td { border-bottom: 0; }
  .fp-t tr[data-best="1"] td { background: color-mix(in srgb, var(--fp-accent) 7%, transparent); }
  .fp-num { font-family: var(--fp-mono); white-space: nowrap; font-weight: 650; }
  .fp-t .fp-right { text-align: right; }
  .fp-leg2 { display: block; font-size: 11px; color: var(--fp-soft); margin-top: 1px; }
  .fp-bestpill {
    display: inline-block; margin-left: 6px; font-size: 9px; font-weight: 700;
    letter-spacing: .07em; text-transform: uppercase; color: var(--fp-accent);
    vertical-align: 1px;
  }
  .fp-book {
    font: inherit; font-size: 12px; font-weight: 650; cursor: pointer;
    padding: 5px 10px; border-radius: 8px; white-space: nowrap;
    border: 1px solid var(--fp-accent);
    background: var(--fp-accent); color: var(--fp-accent-ink);
  }
  .fp-book:hover { filter: brightness(1.07); }
  .fp-book:focus-visible { outline: 2px solid var(--fp-ink); outline-offset: 2px; }
  .fp-nolink { color: var(--fp-faint); font-size: 12px; }

  /* The whole band, in one line, for a frame too narrow to spend 70px of
     a 780px budget on a bar. Same three numbers, same verdict colour. */
  .fp-bandline {
    padding: 0 16px 10px; font-size: 12px; color: var(--fp-soft);
  }
  .fp-bandline .fp-range { font-family: var(--fp-mono); color: var(--fp-ink); }

  /* ── "showing 10 of 34" ── */
  .fp-more {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 10px; padding: 8px 16px 2px; font-size: 11px; color: var(--fp-faint);
  }
  .fp-morebtn {
    font: inherit; font-size: 11px; font-weight: 650; cursor: pointer;
    background: none; border: 0; padding: 2px 0; color: var(--fp-accent);
    text-decoration: underline; white-space: nowrap;
  }
  .fp-morebtn:focus-visible { outline: 2px solid var(--fp-ink); outline-offset: 2px; }

  /* ── notices ── */
  .fp-note {
    margin: 0 16px 12px; padding: 9px 11px; border-radius: 10px;
    background: var(--fp-warn-bg); color: var(--fp-warn-ink); font-size: 12px;
  }
  .fp-empty { padding: 4px 16px 16px; color: var(--fp-soft); font-size: 13px; }
  .fp-ft {
    padding: 9px 16px 12px; font-size: 11px; color: var(--fp-faint);
    border-top: 1px solid var(--fp-line-soft);
  }
  .fp-skel { padding: 16px; color: var(--fp-faint); font-size: 13px; }

  /* Under ~520px a six-column table stops being a table and starts being a
     horizontal scroll nobody scrolls, so each row becomes a block. */
  @media (max-width: 519px) {
    .fp-t thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .fp-t, .fp-t tbody, .fp-t tr, .fp-t td { display: block; width: 100%; }
    .fp-t tr { padding: 4px 0; border-bottom: 1px solid var(--fp-line-soft); }
    .fp-t tr:last-child { border-bottom: 0; }
    .fp-t td { border: 0; padding: 2px 16px; }
    .fp-t td.fp-right { text-align: left; padding-top: 6px; padding-bottom: 8px; }
    .fp-t td[data-label]::before {
      content: attr(data-label) " ";
      font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
      color: var(--fp-faint);
    }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>
</head>
<body>
<div id="fp-root" class="fp-card"><div class="fp-skel">Loading fares…</div></div>
<script>
(function () {
  "use strict";

  /* ── host bridge ────────────────────────────────────────────────────
     Three hosts, one render path.
     * MCP Apps (stable 2026-01-26): send a `ui/initialize` REQUEST, apply
       hostContext.theme, then notify `ui/notifications/initialized`; the
       host answers with `ui/notifications/tool-result`.
     * Draft-era MCP hosts: ignore the unknown request, so a grace timeout
       falls back to the bare `initialized` notification.
     * ChatGPT (OpenAI Apps): no handshake at all -- `window.openai` is
       injected and the payload arrives as `toolOutput`, immediately or on
       an `openai:set_globals` event.
     Both paths converge on renderOnce, guarded by a flag. */
  function post(msg) { try { window.parent.postMessage(msg, "*"); } catch (e) {} }
  var nextId = 1, pending = {};
  function request(method, params, cb) {
    var id = nextId++;
    pending[id] = cb || function () {};
    post({ jsonrpc: "2.0", id: id, method: method, params: params });
  }
  function sizeChanged() {
    var h = document.body.scrollHeight;
    if (h) post({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { width: 400, height: h } });
  }
  var sentInit = false;
  function sendInitialized() {
    if (sentInit) return;
    sentInit = true;
    post({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    sizeChanged();
  }
  request("ui/initialize", {
    appInfo: { name: "flightpowers-flights-card", version: "1.0.0" },
    appCapabilities: {},
    protocolVersion: "2026-01-26"
  }, function (err, result) {
    if (result && result.hostContext && result.hostContext.theme) {
      document.documentElement.setAttribute("data-theme", result.hostContext.theme);
    }
    sendInitialized();
  });
  if (document.readyState === "complete") setTimeout(sendInitialized, 400);
  else window.addEventListener("load", function () { setTimeout(sendInitialized, 400); });
  setTimeout(sendInitialized, 900);

  /* ── what may be opened ──────────────────────────────────────────────
     `buy_link` is upstream data, and the host's link opener is the one
     place this widget could do harm, so the gate is an ALLOWLIST rather
     than a scheme check. https only (an http: booking link would be a
     downgrade we handed the user), and only the booking domains our own
     backends emit: Google Flights buy links, Booking.com room links, and
     Stay22 redirects. A row whose link is anything else renders its price
     with no button at all -- never a button that goes somewhere else.
     Subdomains of each are allowed (`www.google.com`); a lookalike like
     `google.com.evil.test` is not, because the match is on the full host
     or on a dot-prefixed suffix. */
  var LINK_HOSTS = ["google.com", "booking.com", "stay22.com"];
  function allowedLink(url) {
    if (typeof url !== "string" || !url) return false;
    var u;
    try { u = new URL(url.trim()); } catch (e) { return false; }
    if (u.protocol !== "https:") return false;
    var host = (u.hostname || "").toLowerCase();
    for (var i = 0; i < LINK_HOSTS.length; i++) {
      var d = LINK_HOSTS[i];
      if (host === d || host.slice(-(d.length + 1)) === "." + d) return true;
    }
    return false;
  }
  function openLink(url) {
    if (!allowedLink(url)) return;
    if (window.openai && typeof window.openai.openExternal === "function") {
      try { window.openai.openExternal({ href: url }); return; } catch (e) {}
    }
    post({ jsonrpc: "2.0", id: "open-link-" + Date.now(), method: "ui/open-link", params: { url: url } });
  }

  /* ── reading the payload ─────────────────────────────────────────────
     Nothing below assumes a field exists. The server injects no widget
     fields into the tool result (that is the point -- a non-UI client sees
     exactly the JSON it saw before), so every value here is read off the
     response the API has always returned, and a missing one drops its
     element rather than printing "undefined". */
  function num(v) {
    if (typeof v === "number" && isFinite(v)) return v;
    if (typeof v === "string") {
      var n = parseFloat(v.replace(/[^0-9.\-]/g, ""));
      return isFinite(n) ? n : null;
    }
    return null;
  }
  function txt(v) { return (typeof v === "string" && v.trim()) ? v.trim() : ""; }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  /* `price` on a one-way row, `total_price` on a round trip -- both are
     display strings the backend already formatted ("$231"). */
  function priceString(r) { return txt(r.total_price) || txt(r.price); }
  function priceNumber(r) {
    var n = num(r.total_price_as_number);
    if (n === null) n = num(r.price_as_number);
    if (n === null) n = num(priceString(r));
    return n;
  }
  /* price_insights_* are bare numbers while `price` is "$231"/"€231", so
     the symbol has to be read off the row's own price string. No price
     string means the range prints unprefixed rather than labelled with a
     currency nobody stated. */
  function symbolOf(rows) {
    for (var i = 0; i < rows.length; i++) {
      var s = priceString(rows[i]), out = "";
      for (var j = 0; j < s.length; j++) {
        if (/[0-9]/.test(s[j])) break;
        out += s[j];
      }
      if (out) return out.trim();
    }
    return "";
  }
  function money(v, sym) {
    if (v === null) return "";
    var r = Math.round(v);
    return sym + r.toLocaleString("en-US");
  }
  function stopsText(v) {
    var n = num(v);
    if (n === null) return txt(v) || "";
    if (n === 0) return "nonstop";
    return n === 1 ? "1 stop" : n + " stops";
  }
  function isRoundTrip(rows) {
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r && (r.total_price != null || r.departure_flight_departure_description != null
                || r.return_flight_departure_description != null)) return true;
    }
    return false;
  }

  /* ── the price band ──────────────────────────────────────────────────
     Google's own tracking for this route: price_insights_low /
     price_insights_high are the edges of what it calls typical, and
     price_range_in_relation_to_other_periods is its verdict on the fare.
     Drawn only when both numbers are there -- an invented band would be a
     made-up metric, and a bar with nothing behind it is worse than no bar. */
  function bandOf(rows, cheapest, cheapestRow, routes) {
    /* Google tracks each route separately, so the band is the CHEAPEST
       route's own -- the route the marker's fare is on -- and RESTRICTED
       to it: no insights there means no band, never another route's
       numbers under this marker. */
    var pool = rows, dest = cheapestRow ? destOf(cheapestRow) : "";
    if (dest) {
      pool = rows.filter(function (r) { return destOf(r) === dest; });
      pool.unshift(cheapestRow);
    } else if (routes > 1) {
      /* Unnamed cheapest fare with other routes present: every band on
         offer belongs to one of THEM (measured: a $90 no-destination row
         marked against another route's 250-420 band). Only when there is
         something to mix with -- a table where NO row names a route is
         one route as far as anyone can tell, and keeps its band. */
      return null;
    }
    for (var i = 0; i < pool.length; i++) {
      var lo = num(pool[i].price_insights_low), hi = num(pool[i].price_insights_high);
      if (lo !== null && hi !== null && hi > lo) {
        return {
          low: lo,
          high: hi,
          verdict: txt(pool[i].price_range_in_relation_to_other_periods).toLowerCase(),
          fare: cheapest,
          dest: destLabel(dest)
        };
      }
    }
    return null;
  }
  /* The CSS breakpoint, asked in JavaScript: below it the rows are
     blocks and the band is one line. Both have to agree. */
  function isNarrow() {
    try { return window.matchMedia("(max-width: 519px)").matches; }
    catch (e) { return false; }
  }
  function renderBandLine(b, sym, routes) {
    var wrap = el("div", "fp-bandline");
    wrap.appendChild(document.createTextNode("Google: "));
    if (b.verdict === "low" || b.verdict === "typical" || b.verdict === "high") {
      var v = el("span", "fp-verdict", b.verdict);
      v.setAttribute("data-v", b.verdict);
      wrap.appendChild(v);
      wrap.appendChild(document.createTextNode(" "));
    }
    wrap.appendChild(el("span", "fp-range",
      money(b.low, sym) + "–" + money(b.high, sym)));
    if (routes > 1 && b.dest) {
      wrap.appendChild(el("span", null, " · cheapest of " + routes + " routes (" + b.dest + ")"));
    }
    return wrap;
  }
  function renderBand(b, sym, routes) {
    if (isNarrow()) return renderBandLine(b, sym, routes);
    var wrap = el("div", "fp-band");
    var span = b.high - b.low;
    var start = Math.min(b.low - span * 0.35, b.fare === null ? Infinity : b.fare - span * 0.12);
    var end = Math.max(b.high + span * 0.35, b.fare === null ? -Infinity : b.fare + span * 0.12);
    if (!isFinite(start) || !isFinite(end) || end <= start) { start = b.low - span; end = b.high + span; }
    var total = end - start;
    var pct = function (v) { return Math.max(0, Math.min(100, ((v - start) / total) * 100)); };

    /* One route: "for this route", as before. Several: say out loud that
       this is one of them -- the cheapest one -- rather than letting a
       band that covers a third of the table read as if it covered all of
       it. */
    var many = routes > 1;
    var label = many
      ? "Google price tracking · cheapest of " + routes + " routes"
        + (b.dest ? " (" + b.dest + ")" : "")
      : "Google price tracking for this route";
    var top = el("div", "fp-band-top");
    top.appendChild(el("span", null, label));
    if (b.verdict === "low" || b.verdict === "typical" || b.verdict === "high") {
      var v = el("span", "fp-verdict", b.verdict);
      v.setAttribute("data-v", b.verdict);
      top.appendChild(v);
    }
    wrap.appendChild(top);

    var bar = el("div", "fp-bar");
    var lowPct = pct(b.low), highPct = pct(b.high);
    [["low", lowPct], ["typical", highPct - lowPct], ["high", 100 - highPct]].forEach(function (s) {
      var seg = el("div", "fp-seg");
      seg.setAttribute("data-s", s[0]);
      seg.style.width = s[1] + "%";
      bar.appendChild(seg);
    });
    if (b.fare !== null) {
      var m = el("div", "fp-mark");
      m.style.left = pct(b.fare) + "%";
      bar.appendChild(m);
    }
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label",
      (many
        ? "Google tracks " + (b.dest || "the cheapest of these " + routes + " routes")
          + ", the route with the cheapest fare of these " + routes + ", between "
        : "Google tracks this route between ")
      + money(b.low, sym) + " and " + money(b.high, sym) +
      (b.fare !== null ? "; the cheapest fare here is " + money(b.fare, sym) : "") +
      (b.verdict ? ", which Google marks " + b.verdict : "") + ".");
    wrap.appendChild(bar);

    var scale = el("div", "fp-scale");
    scale.appendChild(el("span", null, money(b.low, sym) + " low"));
    scale.appendChild(el("span", null, money(b.high, sym) + " high"));
    wrap.appendChild(scale);
    return wrap;
  }

  /* ── destinations (the why is in the module docstring) ──
     Grouped on the RAW `to_airport`; only the LABEL is shortened. An
     upstream string gets an upstream string's treatment: a 400-character
     one in a nowrap cell was a 3,370px sideways scroll, so the label is
     capped and the full value lives on the cell's title. */
  function destOf(r) { return txt(r.to_airport); }
  //: An upstream string, so it gets an upstream string's treatment: a
  //: 400-character `to_airport` in a nowrap cell was a 3,370px sideways
  //: scroll. The full value stays available as the cell's title.
  var LABEL_MAX = 16;
  function destLabel(v) {
    var m = /\(([A-Za-z0-9]{3})\)\s*$/.exec(v);
    var out = m ? m[1].toUpperCase() : v;
    return out.length > LABEL_MAX
      ? out.slice(0, LABEL_MAX - 1).replace(/\s+$/, "") + "…"
      : out;
  }
  /* Every row lands in exactly one bucket, first-seen order, unlabelled
     rows last: `dest: ""` IS the Other bucket, so nothing downstream has
     to remember the special case. */
  function bucketsOf(rows) {
    /* Null-prototype map: a destination called "constructor" is a
       destination like any other, not a hit on Object.prototype. */
    var byDest = Object.create(null), order = [], other = [];
    rows.forEach(function (r) {
      var d = destOf(r);
      if (!d) { other.push(r); return; }
      if (!byDest[d]) { byDest[d] = []; order.push(d); }
      byDest[d].push(r);
    });
    var out = order.map(function (d) { return { dest: d, rows: byDest[d] }; });
    if (other.length) out.push({ dest: "", rows: other });
    return out;
  }
  function renderPills(buckets, allRows, onPick) {
    var wrap = el("div", "fp-pills");
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Filter fares by destination");
    var buttons = [];
    function add(label, rows, title) {
      var b = el("button", "fp-pill", label);
      b.type = "button";
      b.setAttribute("aria-pressed", "false");
      if (title && title !== label) b.setAttribute("title", title);
      b.appendChild(el("span", "fp-pill-n", rows.length));
      b.addEventListener("click", function () {
        buttons.forEach(function (o) { o.setAttribute("aria-pressed", "false"); });
        b.setAttribute("aria-pressed", "true");
        onPick(rows);
      });
      buttons.push(b);
      wrap.appendChild(b);
      return b;
    }
    /* All is the default and it is selected: the card opens showing the
       whole answer, exactly as it did before there were pills. */
    add("All", allRows).setAttribute("aria-pressed", "true");
    buckets.forEach(function (g) {
      add(g.dest ? destLabel(g.dest) : "Other", g.rows, g.dest);
    });
    return wrap;
  }

  /* ── the table ── */
  function bookCell(r) {
    var td = el("td", "fp-right");
    td.setAttribute("data-label", "");
    var url = txt(r.buy_link);
    if (!allowedLink(url)) { td.appendChild(el("span", "fp-nolink", "—")); return td; }
    var b = el("button", "fp-book", "Book →");
    b.type = "button";
    b.setAttribute("aria-label", "Book this flight for " + (priceString(r) || "the listed fare"));
    b.addEventListener("click", function () { openLink(url); });
    td.appendChild(b);
    return td;
  }
  function cell(label, main, sub, cls) {
    var td = el("td", cls || null);
    td.setAttribute("data-label", label);
    td.appendChild(document.createTextNode(main || "—"));
    if (sub) td.appendChild(el("span", "fp-leg2", sub));
    return td;
  }
  /* The label is capped, so the cell carries the full upstream value as
     its title rather than losing it. A row with no destination shows the
     same em dash every other empty cell shows. */
  function destCell(r) {
    var raw = destOf(r);
    var td = cell("To", raw ? destLabel(raw) : "", "", "fp-dest");
    if (raw) td.setAttribute("title", raw);
    return td;
  }
  function renderTable(rows, rt, cheapest, showDest) {
    /* Only the FIRST row at the cheapest price is badged. A date-range
       search routinely returns two identical fares (same carrier, two
       departure times) and badging both says "cheapest" twice. */
    var badged = false;
    /* Only while the table holds more than one destination: filtered to
       one, the column is the same three letters all the way down and one
       more column squeezing the six that carry the fare. */
    var headers = rt
      ? ["Outbound", "Return", "Airline", "Stops", "Total", "Book"]
      : ["Depart", "Airline", "Stops", "Duration", "Price", "Book"];
    if (showDest) headers.splice(rt ? 2 : 1, 0, "To");
    var table = el("table", "fp-t");
    table.setAttribute("aria-label", rt ? "Round-trip fares" : "One-way fares");
    var thead = el("thead"), htr = el("tr");
    headers.forEach(function (h) {
      var th = el("th", null, h);
      th.scope = "col";
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = el("tbody");
    var rowEls = [];
    rows.forEach(function (r) {
      var tr = el("tr");
      rowEls.push(tr);
      var mine = priceNumber(r);
      var best = cheapest !== null && mine !== null && mine === cheapest && !badged;
      if (best) { badged = true; tr.setAttribute("data-best", "1"); }
      if (rt) {
        tr.appendChild(cell("Outbound", txt(r.departure_flight_departure_description) || txt(r.departure_date),
                            txt(r.departure_flight_duration), "fp-when"));
        tr.appendChild(cell("Return", txt(r.return_flight_departure_description) || txt(r.return_date),
                            txt(r.return_flight_duration), "fp-when"));
        if (showDest) tr.appendChild(destCell(r));
        var back = txt(r.return_flight_airline), out = txt(r.departure_flight_airline);
        tr.appendChild(cell("Airline", out, (back && back !== out) ? "back: " + back : ""));
        tr.appendChild(cell("Stops", stopsText(r.total_stops)));
      } else {
        tr.appendChild(cell("Depart", txt(r.departure_description) || txt(r.departure_date),
                            txt(r.arrival_description) ? "arrives " + txt(r.arrival_description) : "",
                            "fp-when"));
        if (showDest) tr.appendChild(destCell(r));
        tr.appendChild(cell("Airline", txt(r.airline)));
        tr.appendChild(cell("Stops", stopsText(r.stops)));
        tr.appendChild(cell("Duration", txt(r.duration), "", "fp-dur"));
      }
      var pc = cell(rt ? "Total" : "Price", priceString(r), "", "fp-num");
      if (best) pc.appendChild(el("span", "fp-bestpill", "cheapest"));
      tr.appendChild(pc);
      tr.appendChild(bookCell(r));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    var scroll = el("div", "fp-scroll");
    scroll.appendChild(table);
    return { scroll: scroll, rowEls: rowEls };
  }

  /* ── how tall the card is allowed to be ──
     Ten rows by default, the rest one scroll away. Measured, not guessed:
     the cap is the distance from the top of the scroll box to the top of
     row eleven, then trimmed again while the whole card is over
     MAX_CARD_PX. Returns how many rows ended up FULLY visible, because
     the footer says that number out loud and "10" over eight rows would
     be a made-up figure. */
  var MAX_ROWS_SHOWN = 10;
  /* The floor: on a 380px frame a row is a 184px block and the ceiling
     alone left TWO fares on screen. The ceiling gives way to this, not
     the other way round. */
  var MIN_ROWS_SHOWN = 5;
  /* Ten unwrapped rows are ~510px of table on their own, so a card that
     shows ten of them lands near 780 with the header, band, pills and
     footer on top. That is the ceiling, not the target: it exists for the
     layouts where a row is 90px (a narrow frame, the block layout), where
     ten rows would be 1,100px of chat window and the card shows as many
     as fit instead -- and says how many. */
  var MAX_CARD_PX = 780;
  var MIN_ROWS_PX = 120;
  function capRows(scroll, px) {
    scroll.setAttribute("data-capped", "1");
    scroll.style.maxHeight = px + "px";
  }
  function fitRows(scroll, rowEls) {
    /* Re-runnable, and it undoes its own last answer first: paint() runs
       it again once the "showing N of M" line is in the DOM, because that
       line is part of the card being fitted. */
    uncapRows(scroll);
    var n = rowEls.length;
    if (!n) return 0;
    var box = scroll.getBoundingClientRect();
    var top = box.top, natural = box.height;
    /* Height of the first `i` rows, measured from the top of the scroll
       box (so it includes the sticky header). `i === n` means "all of
       them", which has no row to measure against. */
    function span(i) {
      return i < n
        ? Math.ceil(rowEls[i].getBoundingClientRect().top - top)
        : Math.ceil(natural);
    }
    var cap = span(Math.min(MAX_ROWS_SHOWN, n));
    /* No layout at all (a host that renders the frame with zero height,
       a measurement taken while hidden): leave the table uncapped rather
       than collapse it to nothing. */
    if (!(cap > 0)) return n;
    var floor = Math.max(MIN_ROWS_PX, span(Math.min(MIN_ROWS_SHOWN, n)));
    capRows(scroll, cap);
    /* Not once: capping can bring a scrollbar or a reflowed footer with
       it. Bounded, because a loop that fights its own layout spins.
       Applied whatever the row count -- ten 184px block rows are a
       2,029px card, and "ten rows" never meant "short". */
    for (var pass = 0; pass < 3; pass++) {
      var over = document.body.scrollHeight - MAX_CARD_PX;
      if (over <= 0) break;
      var next = Math.max(floor, cap - over);
      if (next >= cap) break;
      cap = next;
      capRows(scroll, cap);
    }
    if (cap >= natural - 0.5) {
      /* Everything fits: no cap, no scrollbar, no footer line. */
      uncapRows(scroll);
      return n;
    }
    var bottom = scroll.getBoundingClientRect().bottom;
    var visible = 0;
    for (var i = 0; i < n; i++) {
      if (rowEls[i].getBoundingClientRect().bottom > bottom + 0.5) break;
      visible++;
    }
    return visible || 1;
  }
  function uncapRows(scroll) {
    scroll.removeAttribute("data-capped");
    scroll.style.maxHeight = "";
  }
  /* The footer line under a capped table. The button is not decoration:
     a host that swallows the inner scroll would otherwise leave the
     hidden rows unreachable. Built empty and filled in afterwards: it is
     part of the card `fitRows` measures, and the number it prints is not
     known until the fit has happened. */
  function moreLine(total, expanded, onToggle) {
    var wrap = el("div", "fp-more");
    wrap.appendChild(el("span", "fp-more-t", ""));
    var b = el("button", "fp-morebtn", expanded
      ? "Show top " + MAX_ROWS_SHOWN
      : "Show all " + total);
    b.type = "button";
    b.addEventListener("click", onToggle);
    wrap.appendChild(b);
    return wrap;
  }
  function setMoreText(wrap, shown, total, expanded) {
    wrap.firstChild.textContent = expanded
      ? "Showing all " + total + " fares"
      : "Showing " + shown + " of " + total + " · scroll for more";
  }

  /* ── header + footer ── */
  function headerOf(sc, rows) {
    var cov = (sc && typeof sc.search_coverage === "object" && sc.search_coverage) || {};
    var from = rows.length ? txt(rows[0].from_airport) : "";
    var dests = Array.isArray(cov.destinations_searched) ? cov.destinations_searched.filter(txt) : [];
    if (!dests.length && rows.length) {
      rows.forEach(function (r) {
        var t = txt(r.to_airport);
        if (t && dests.indexOf(t) === -1) dests.push(t);
      });
    }
    var route = from && dests.length ? from + " → " + dests.join(", ")
              : (dests.length ? dests.join(", ") : (from || "Flight search"));
    var bits = [];
    var n = typeof sc.result_count === "number" ? sc.result_count : rows.length;
    if (n) bits.push(n === 1 ? "1 fare" : n + " fares");
    var dates = Array.isArray(cov.departure_dates_searched) ? cov.departure_dates_searched.length : 0;
    if (dates > 1) bits.push("across " + dates + " dates");
    else if (dates === 1) bits.push("on " + txt(cov.departure_dates_searched[0]));
    var hd = el("div", "fp-hd");
    hd.appendChild(el("div", "fp-route", route));
    if (bits.length) hd.appendChild(el("div", "fp-sub", bits.join(" · ")));
    return hd;
  }
  function footerOf(sc) {
    var u = (sc && typeof sc.api_usage === "object" && sc.api_usage) || {};
    var bits = [];
    var used = num(u.requests_used_by_this_call);
    if (used !== null) bits.push(used === 1 ? "1 request billed" : used + " requests billed");
    var left = num(u.plan_requests_remaining);
    if (left !== null) bits.push(left.toLocaleString("en-US") + " left on your plan");
    if (!bits.length) return null;
    return el("div", "fp-ft", bits.join(" · "));
  }

  /* ── render ── */
  function render(sc) {
    var root = document.getElementById("fp-root");
    while (root.firstChild) root.removeChild(root.firstChild);
    var rows = Array.isArray(sc.results) ? sc.results.filter(function (r) { return r && typeof r === "object"; }) : [];
    var status = txt(sc.search_status);

    root.appendChild(headerOf(sc, rows));

    /* The non-search exits are returned as data on purpose (a model has to
       relay them to a human), so the card relays them too instead of
       drawing an empty table. */
    var msg = txt(sc.message) || txt(sc.partial);
    if (sc.needs_api_key === true || sc.quota_exhausted === true
        || status === "degraded" || status === "trial_exhausted" || status === "empty" || msg) {
      if (msg) root.appendChild(el("div", "fp-note", msg));
    }

    if (!rows.length) {
      if (!msg) {
        root.appendChild(el("div", "fp-empty",
          status === "empty" ? "No flights found for this search."
                             : "No fares to show."));
      }
      return;
    }

    /* `selected` is whichever destination's rows the pills are showing --
       all of them until someone clicks. The band, the cheapest marker and
       the counts are all recomputed from it: Google's price band is per
       route, so drawing Rome's band over Athens' fares would be a number
       we invented. */
    var body = el("div", "fp-body");
    var selected = rows;
    var expanded = false;

    function paint() {
      while (body.firstChild) body.removeChild(body.firstChild);
      var sym = symbolOf(selected);
      var cheapest = null, cheapestRow = null;
      selected.forEach(function (r) {
        var n = priceNumber(r);
        if (n !== null && (cheapest === null || n < cheapest)) { cheapest = n; cheapestRow = r; }
      });
      /* How many routes are in the table RIGHT NOW: three under All, one
         under a pill. It decides both the destination column and what the
         band is allowed to claim. */
      var routes = bucketsOf(selected).length;
      var band = bandOf(selected, cheapest, cheapestRow, routes);
      if (band) body.appendChild(renderBand(band, sym, routes));
      var built = renderTable(selected, isRoundTrip(selected), cheapest, routes > 1);
      body.appendChild(built.scroll);

      var total = selected.length;
      function toggle() { expanded = !expanded; paint(); }
      var shown = total, more = null;
      if (expanded) {
        uncapRows(built.scroll);
        more = moreLine(total, true, toggle);
        body.appendChild(more);
        setMoreText(more, total, total, true);
      } else {
        /* The fit decides whether anything is hidden -- not "more than
           ten", because on a narrow frame the ceiling hides rows out of
           eight -- and then runs again, because the line it produced is
           part of the card. */
        shown = fitRows(built.scroll, built.rowEls);
        if (shown < total) {
          more = moreLine(total, false, toggle);
          body.appendChild(more);
          shown = fitRows(built.scroll, built.rowEls);
          setMoreText(more, shown, total, false);
        }
      }
      sizeChanged();
    }

    var buckets = bucketsOf(rows);
    if (buckets.length > 1) {
      root.appendChild(renderPills(buckets, rows, function (picked) {
        selected = picked;
        expanded = false;
        paint();
      }));
    }
    root.appendChild(body);
    /* Before the first paint, not after: the height trim measures the
       whole document, and a footer appended later would push the card
       past the budget it was just fitted to. */
    var ft = footerOf(sc);
    if (ft) root.appendChild(ft);
    paint();
  }

  function renderOnce(sc) {
    if (window.__fpRendered || !sc || typeof sc !== "object") return;
    window.__fpRendered = true;
    try { render(sc); } catch (e) {
      var root = document.getElementById("fp-root");
      if (root && !root.firstChild) root.appendChild(el("div", "fp-skel", "Fares are in the response below."));
    }
    sizeChanged();
  }

  function tryOpenAI() {
    var oa = window.openai;
    if (oa && oa.toolOutput) renderOnce(oa.toolOutput);
  }
  window.addEventListener("openai:set_globals", function (ev) {
    var g = ev && ev.detail && ev.detail.globals;
    renderOnce((g && g.toolOutput) || (window.openai && window.openai.toolOutput));
  });
  tryOpenAI();
  setTimeout(tryOpenAI, 300);
  window.addEventListener("load", tryOpenAI);

  window.addEventListener("message", function (ev) {
    /* Gate on source, never origin: sandbox proxies vary the origin, but
       the sender window is fixed. A NULL source is refused too -- it is
       what a message relayed from a detached context or a worker looks
       like, and the tool result is the one input that decides what this
       frame renders and what its buttons open. */
    if (!ev.source || ev.source !== window.parent) return;
    var data = ev && ev.data;
    if (!data) return;
    if (data.id != null && data.method == null) {
      var cb = pending[data.id];
      delete pending[data.id];
      if (cb) cb(data.error || null, data.result);
      return;
    }
    if (data.method === "ui/resource-teardown" && data.id != null) {
      /* The spec wants an answer or the host logs the view as hung. */
      post({ jsonrpc: "2.0", id: data.id, result: {} });
      return;
    }
    if (data.method !== "ui/notifications/tool-result") return;
    renderOnce((data.params || {}).structuredContent);
  });
})();
</script>
</body>
</html>
"""
