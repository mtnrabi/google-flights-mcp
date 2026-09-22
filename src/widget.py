"""The flights result card: one self-contained MCP Apps widget, no ads.

What this is
------------
A host that renders MCP UI (claude.ai, ChatGPT) asks the server for an
HTML resource and hands the tool's `structuredContent` to it over a
postMessage bridge. This module is that resource for the two flights
tools: a compact fare table, Google's own price band drawn as a
three-segment bar with the verdict and the cheapest fare marked on it,
and a Book button per row.

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

  /* ── the table ── */
  .fp-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table.fp-t { width: 100%; border-collapse: collapse; font-size: 13px; }
  .fp-t th {
    text-align: left; font-size: 10px; font-weight: 700; letter-spacing: .09em;
    text-transform: uppercase; color: var(--fp-faint);
    padding: 6px 10px; border-top: 1px solid var(--fp-line);
    border-bottom: 1px solid var(--fp-line); white-space: nowrap;
  }
  .fp-t td {
    padding: 9px 10px; border-bottom: 1px solid var(--fp-line-soft);
    vertical-align: top;
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

  /* Under ~440px a six-column table stops being a table and starts being a
     horizontal scroll nobody scrolls, so each row becomes a block. */
  @media (max-width: 440px) {
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
  function bandOf(rows, cheapest) {
    for (var i = 0; i < rows.length; i++) {
      var lo = num(rows[i].price_insights_low), hi = num(rows[i].price_insights_high);
      if (lo !== null && hi !== null && hi > lo) {
        return { low: lo, high: hi, verdict: txt(rows[i].price_range_in_relation_to_other_periods).toLowerCase(), fare: cheapest };
      }
    }
    return null;
  }
  function renderBand(b, sym) {
    var wrap = el("div", "fp-band");
    var span = b.high - b.low;
    var start = Math.min(b.low - span * 0.35, b.fare === null ? Infinity : b.fare - span * 0.12);
    var end = Math.max(b.high + span * 0.35, b.fare === null ? -Infinity : b.fare + span * 0.12);
    if (!isFinite(start) || !isFinite(end) || end <= start) { start = b.low - span; end = b.high + span; }
    var total = end - start;
    var pct = function (v) { return Math.max(0, Math.min(100, ((v - start) / total) * 100)); };

    var top = el("div", "fp-band-top");
    top.appendChild(el("span", null, "Google price tracking for this route"));
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
      "Google tracks this route between " + money(b.low, sym) + " and " + money(b.high, sym) +
      (b.fare !== null ? "; the cheapest fare here is " + money(b.fare, sym) : "") +
      (b.verdict ? ", which Google marks " + b.verdict : "") + ".");
    wrap.appendChild(bar);

    var scale = el("div", "fp-scale");
    scale.appendChild(el("span", null, money(b.low, sym) + " low"));
    scale.appendChild(el("span", null, money(b.high, sym) + " high"));
    wrap.appendChild(scale);
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
  function renderTable(rows, rt, cheapest) {
    /* Only the FIRST row at the cheapest price is badged. A date-range
       search routinely returns two identical fares (same carrier, two
       departure times) and badging both says "cheapest" twice. */
    var badged = false;
    var headers = rt
      ? ["Outbound", "Return", "Airline", "Stops", "Total", "Book"]
      : ["Depart", "Airline", "Stops", "Duration", "Price", "Book"];
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
    rows.forEach(function (r) {
      var tr = el("tr");
      var mine = priceNumber(r);
      var best = cheapest !== null && mine !== null && mine === cheapest && !badged;
      if (best) { badged = true; tr.setAttribute("data-best", "1"); }
      if (rt) {
        tr.appendChild(cell("Outbound", txt(r.departure_flight_departure_description) || txt(r.departure_date),
                            txt(r.departure_flight_duration)));
        tr.appendChild(cell("Return", txt(r.return_flight_departure_description) || txt(r.return_date),
                            txt(r.return_flight_duration)));
        var back = txt(r.return_flight_airline), out = txt(r.departure_flight_airline);
        tr.appendChild(cell("Airline", out, (back && back !== out) ? "back: " + back : ""));
        tr.appendChild(cell("Stops", stopsText(r.total_stops)));
      } else {
        tr.appendChild(cell("Depart", txt(r.departure_description) || txt(r.departure_date),
                            txt(r.arrival_description) ? "arrives " + txt(r.arrival_description) : ""));
        tr.appendChild(cell("Airline", txt(r.airline)));
        tr.appendChild(cell("Stops", stopsText(r.stops)));
        tr.appendChild(cell("Duration", txt(r.duration)));
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
    return scroll;
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

    var sym = symbolOf(rows);
    var cheapest = null;
    rows.forEach(function (r) {
      var n = priceNumber(r);
      if (n !== null && (cheapest === null || n < cheapest)) cheapest = n;
    });
    var band = bandOf(rows, cheapest);
    if (band) root.appendChild(renderBand(band, sym));
    root.appendChild(renderTable(rows, isRoundTrip(rows), cheapest));
    var ft = footerOf(sc);
    if (ft) root.appendChild(ft);
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
