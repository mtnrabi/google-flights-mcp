"""The flights result card: one self-contained MCP Apps widget, no ads.

What this is
------------
A host that renders MCP UI (claude.ai, ChatGPT) asks the server for an
HTML resource and hands the tool's `structuredContent` to it over a
postMessage bridge. This module is that resource for the two flights
tools: a compact fare table, Google's own price band drawn as a
three-segment bar with the verdict and the cheapest fare marked on it,
and a Book button per row.

It draws the FIVE cheapest rows and nothing else, with one button that
appends the next five ("Show 5 more / 274 left") and a "Show fewer" link
back to five. There is no scroll container and no height ceiling: the card
is exactly as tall as the rows on screen and the HOST scrolls the page.
That is the 2026-09-22 correction, in two steps. First, watching a demo
scroll a 93-row answer inside the message: "wtf is that long scroll. limit
the presented items to the top 10, and allow the user to tap on a button
to view more." Then, on the ten-row card: "make it top 5, still seems too
long." A scrollbar inside a chat message is a trap; a button is not, and
five fares plus a button is a card you can take in at a glance. Below
520px the band collapses to one line, because a 70px bar in a phone-width
frame is height that should be a fare.

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

The look: ours, not Google's
----------------------------
Matan, 2026-09-22, on the version before this one: *"widget UI is WAYYYY
too similar to google flights. do dark mode something cooler with
flightpowers logo."* He was right -- a white table of fares with blue pill
buttons IS Google Flights, and a card that looks like the thing it is
reading from has no reason to exist.

So this card commits to ONE look on every host. A light claude.ai and a
dark one get the same dark card: the ground is painted explicitly,
`color-scheme: dark` stops Chromium sliding a white sheet under it, the
host's `hostContext.theme` is read and ignored, and there is no
`prefers-color-scheme` branch to drift.

The palette is the site's own (`flightpowers-developers/src/app/globals.css`,
copied by value because this frame loads nothing): `ink-900 #0c0e11` ground,
`ink-800` surfaces, `ink-700` rules, `ink-100/200` text, `ink-400` muted.
ONE accent, `signal-500 #ffb020`, and it is spent only where the answer is
-- the cheapest row carries an amber wash, an amber rule down its edge, an
amber price and a CHEAPEST tag, and the selected destination chip, the
gauge marker and the Show-more button are the same amber. Everything else
is quiet, because an accent on every row is not an accent. Verdict green
and red appear in the price band and nowhere else. Numbers are monospace
and tabular so a column of fares lines up. The header carries the site's
own robot mark, inlined as a data URI and drawn at 28px from a 56px asset.

Two deliberate non-decisions: no lone acid-green pop and no purple
gradient (the two tells of a card a model designed), and no oversized
hero -- the biggest thing on this card is a fare, which is what was asked
for.

Comments and the byte budget
----------------------------
The host inlines this whole document into a sandboxed iframe and the
budget is 40 KB, of which the inlined brand mark is 3.6 KB. So the frame
carries short comments and this docstring carries the reasoning: what is
served is exactly what is written below, with no build step between them.

The host bridge
---------------
Three hosts, one render path.

* MCP Apps (stable 2026-01-26): the frame sends a `ui/initialize` REQUEST,
  then notifies `ui/notifications/initialized`; the host answers with
  `ui/notifications/tool-result` carrying `structuredContent`.
* Draft-era MCP hosts ignore the unknown request, so a grace timeout falls
  back to the bare `initialized` notification.
* ChatGPT (OpenAI Apps) does no handshake at all -- `window.openai` is
  injected and the payload arrives as `toolOutput`, immediately or on an
  `openai:set_globals` event.

Both paths converge on `renderOnce`, guarded by a flag, and a message
whose `source` is not `window.parent` is refused: the tool result is the
one input deciding what this frame draws and what its buttons open.

Links
-----
`buy_link` is upstream data and the host's link opener is the one place
this widget could do harm, so the gate is an ALLOWLIST rather than a
scheme check: https only (an http: booking link would be a downgrade we
handed the user), and only the booking domains our own backends emit --
google.com, booking.com, stay22.com. Subdomains are allowed
(`www.google.com`); a lookalike like `google.com.evil.test` is not,
because the match is on the full host or a dot-prefixed suffix. A row
whose link is anything else renders its price with no button at all,
never a button that goes somewhere else.

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

The `quota_exceeded` refusal
----------------------------
A search the caller's free allowance or plan cannot pay for is refused
outright rather than sampled down (src/quota_gate.py). It arrives here with
no rows, a `search_status` of `quota_exceeded` and the two numbers that
explain it, so the card draws a SMALL block -- "93 requests needed / 10
left" and the message, which already names the two ways forward -- and never
a table. Rendering the usual empty table over a refusal is exactly the
misreading the refusal exists to prevent.
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
_FRAME_SOURCE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlightPowers fares</title>
<style>
  /* ONE look everywhere. `color-scheme: dark` stops Chromium sliding a
     white sheet under the card. No theme branch, on purpose. */
  html { color-scheme: dark; }
  html, body { background: transparent; margin: 0; }
  * { box-sizing: border-box; }

  /* The site's own tokens, copied by value: this frame loads nothing. */
  :root {
    --fp-bg: #0c0e11;        /* ink-900, the card ground */
    --fp-bg-2: #171b21;      /* ink-800, raised surfaces */
    --fp-line: #222831;      /* ink-700, rules */
    --fp-line-2: #171b21;    /* ink-800, hairlines between rows */
    --fp-ink: #e8edf2;       /* ink-100 */
    --fp-ink-2: #c9d1da;     /* ink-200 */
    --fp-soft: #a3adba;      /* ink-300 */
    --fp-faint: #7d8794;     /* ink-400 */
    --fp-accent: #ffb020;    /* signal-500, the ONE accent */
    --fp-accent-hi: #ffc352; /* signal-400 */
    --fp-accent-ink: #0c0e11;
    --fp-low: #4ade80;
    --fp-typical: #ffb020;
    --fp-high: #f87171;
    --fp-track: #222831;
    --fp-warn-bg: #1f1708;
    --fp-warn-ink: #ffc352;
    --fp-radius: 16px;
    --fp-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }

  body {
    font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
          "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    color: var(--fp-ink-2);
  }
  .fp-card {
    background: var(--fp-bg);
    border: 1px solid var(--fp-line);
    border-radius: var(--fp-radius);
    overflow: hidden;
  }
  /* ── brand row: mark, wordmark, then the route ── */
  .fp-brand {
    display: flex; align-items: center; gap: 8px;
    padding: 14px 16px 0;
  }
  /* The robot mark, inlined as a CSS background rather than an image
     element: this frame ships no image tag and no source attribute at
     all, which two tests assert on the served bytes. 56px drawn at 28. */
  .fp-logo {
    width: 28px; height: 28px; display: block; border-radius: 7px;
    background-image: url(data:image/png;base64,__FP_MARK_B64__);
    background-size: 28px 28px; background-repeat: no-repeat;
    flex: 0 0 auto;
  }
  .fp-word {
    font-size: 16px; font-weight: 600; letter-spacing: -.025em;
    color: var(--fp-ink);
  }
  .fp-hd { padding: 8px 16px 12px; }
  .fp-route {
    font-size: 15px; font-weight: 600; letter-spacing: -.01em;
    color: var(--fp-ink);
  }
  .fp-sub {
    font-size: 11px; color: var(--fp-faint); margin-top: 3px;
    letter-spacing: .04em; text-transform: uppercase;
  }

  /* ── the price band, as a gauge: 5px, amber glow on the cheapest ── */
  .fp-band { padding: 2px 16px 14px; }
  .fp-band-top {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 8px; font-size: 11px; color: var(--fp-faint); margin-bottom: 7px;
    letter-spacing: .02em;
  }
  .fp-verdict {
    font-weight: 700; text-transform: uppercase; letter-spacing: .1em;
    font-size: 10px;
  }
  .fp-verdict[data-v="low"] { color: var(--fp-low); }
  .fp-verdict[data-v="typical"] { color: var(--fp-typical); }
  .fp-verdict[data-v="high"] { color: var(--fp-high); }
  .fp-bar {
    position: relative; height: 5px; border-radius: 999px;
    background: var(--fp-track); display: flex; overflow: visible;
  }
  .fp-seg { height: 5px; opacity: .55; }
  .fp-seg:first-child { border-radius: 999px 0 0 999px; }
  .fp-seg:last-child { border-radius: 0 999px 999px 0; }
  .fp-seg[data-s="low"] { background: var(--fp-low); }
  .fp-seg[data-s="typical"] { background: var(--fp-typical); }
  .fp-seg[data-s="high"] { background: var(--fp-high); }
  .fp-mark {
    position: absolute; top: -5px; width: 3px; height: 15px; border-radius: 2px;
    background: var(--fp-accent); transform: translateX(-1.5px);
    box-shadow: 0 0 0 2px var(--fp-bg), 0 0 10px 1px var(--fp-accent);
  }
  .fp-scale {
    display: flex; justify-content: space-between;
    font-size: 10px; color: var(--fp-faint); margin-top: 7px;
    font-family: var(--fp-mono); font-variant-numeric: tabular-nums;
    letter-spacing: .04em;
  }

  /* ── destination pills: one table, one city at a time ── */
  .fp-pills { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 14px; }
  .fp-pill {
    font: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
    padding: 5px 11px; border-radius: 8px;
    border: 1px solid var(--fp-line); background: var(--fp-bg-2);
    color: var(--fp-soft);
  }
  .fp-pill:hover { border-color: var(--fp-faint); color: var(--fp-ink); }
  .fp-pill:focus-visible { outline: 2px solid var(--fp-accent); outline-offset: 2px; }
  .fp-pill[aria-pressed="true"] {
    background: var(--fp-accent); border-color: var(--fp-accent);
    color: var(--fp-accent-ink);
  }
  .fp-pill-n {
    opacity: .7; font-weight: 500; margin-left: 6px;
    font-family: var(--fp-mono); font-variant-numeric: tabular-nums;
  }
  /* Only rendered while the table shows more than one route. */
  /* Bounded in PIXELS as well as characters: the 16-character cap is a
     guess about character width, and it was 23px wrong at 760. */
  .fp-t td.fp-dest {
    font-weight: 700; letter-spacing: .06em; white-space: nowrap;
    color: var(--fp-soft); font-size: 12px;
    max-width: 86px; overflow: hidden; text-overflow: ellipsis;
  }
  @media (max-width: 519px) {
    .fp-t td.fp-dest { max-width: none; }
  }

  /* ── the table ── */
  /* `overflow-x` only, never a max-height: no inner vertical scrollbar
     at all. The Show-more button is what replaced it. */
  .fp-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table.fp-t { width: 100%; border-collapse: collapse; font-size: 13px; }
  .fp-t th {
    text-align: left; font-size: 9px; font-weight: 600; letter-spacing: .14em;
    text-transform: uppercase; color: var(--fp-faint);
    padding: 7px 8px 6px; border-top: 1px solid var(--fp-line);
    border-bottom: 1px solid var(--fp-line); white-space: nowrap;
    background: var(--fp-bg);
  }
  .fp-t td {
    padding: 9px 8px; border-bottom: 1px solid var(--fp-line-2);
    vertical-align: top; color: var(--fp-ink-2);
  }
  /* 740 is MEASURED: a seven-column row with real airline names needs
     774px unwrapped. Below it the times wrap rather than scroll. */
  @media (min-width: 740px) {
    .fp-t td.fp-when, .fp-t td.fp-when .fp-leg2,
    .fp-t td.fp-dur { white-space: nowrap; }
  }
  .fp-t tr:last-child td { border-bottom: 0; }
  /* The cheapest row is LIT: a faint amber wash and an amber rule down
     its left edge. */
  .fp-t tr[data-best="1"] td {
    background: rgba(255, 176, 32, .07);
  }
  .fp-t tr[data-best="1"] td:first-child { box-shadow: inset 2px 0 0 var(--fp-accent); }
  .fp-t tr[data-best="1"] td.fp-num { color: var(--fp-accent); }
  /* Prices are the answer: mono, tabular, biggest thing on the row. */
  /* `.fp-t td` out-specifies a bare `.fp-num`, so this beats it. */
  .fp-num, .fp-t td.fp-num {
    font-family: var(--fp-mono); font-variant-numeric: tabular-nums;
    white-space: nowrap; font-weight: 600; font-size: 16px;
    letter-spacing: -.02em; color: var(--fp-ink);
  }
  .fp-t .fp-right { text-align: right; }
  .fp-leg2 { display: block; font-size: 11px; color: var(--fp-faint); margin-top: 2px; }
  /* Under the price, not beside it: inline it made the column 60px wider
     than its widest fare, which is what wrapped the times at 760. */
  .fp-bestpill {
    display: block; margin: 5px 0 0; font-size: 8px; font-weight: 700;
    letter-spacing: .12em; text-transform: uppercase; color: var(--fp-accent-ink);
    background: var(--fp-accent); border-radius: 4px; padding: 2px 5px;
    width: max-content;
  }
  /* Ghost, not a filled pill: five amber buttons down a column would be
     five things shouting, and only one row is the answer. */
  .fp-book {
    font: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
    padding: 5px 11px; border-radius: 8px; white-space: nowrap;
    border: 1px solid var(--fp-line); background: transparent;
    color: var(--fp-soft);
  }
  .fp-book:hover { border-color: var(--fp-accent); color: var(--fp-accent); }
  .fp-book:focus-visible { outline: 2px solid var(--fp-accent); outline-offset: 2px; }
  .fp-t tr[data-best="1"] .fp-book {
    border-color: var(--fp-accent); background: var(--fp-accent);
    color: var(--fp-accent-ink);
  }
  .fp-t tr[data-best="1"] .fp-book:hover { background: var(--fp-accent-hi); color: var(--fp-accent-ink); }
  .fp-nolink { color: var(--fp-faint); font-size: 12px; }

  /* The whole band, in one line, for a frame too narrow to spend 70px of
     a 780px budget on a bar. Same three numbers, same verdict colour. */
  .fp-bandline {
    padding: 0 16px 12px; font-size: 12px; color: var(--fp-faint);
  }
  .fp-bandline .fp-range {
    font-family: var(--fp-mono); font-variant-numeric: tabular-nums;
    color: var(--fp-ink);
  }

  /* ── "Showing 10 of 93" + the two controls ── */
  /* Full width: the one control under the list, unmissable on a phone. */
  .fp-more { padding: 12px 16px 4px; }
  .fp-more-t {
    display: block; font-size: 10px; color: var(--fp-faint);
    letter-spacing: .1em; text-transform: uppercase; margin-bottom: 8px;
    font-variant-numeric: tabular-nums;
  }
  .fp-more-acts { display: flex; align-items: center; gap: 10px; }
  .fp-morebtn {
    font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
    flex: 1 1 auto; padding: 9px 12px; border-radius: 10px;
    border: 1px solid var(--fp-line); background: var(--fp-bg-2);
    color: var(--fp-accent);
  }
  .fp-morebtn:hover { border-color: var(--fp-accent); background: rgba(255, 176, 32, .08); }
  .fp-morebtn:focus-visible { outline: 2px solid var(--fp-accent); outline-offset: 2px; }
  .fp-fewbtn {
    font: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
    flex: 0 0 auto; padding: 9px 12px; border-radius: 10px;
    border: 1px solid transparent; background: none; color: var(--fp-faint);
    white-space: nowrap;
  }
  .fp-fewbtn:hover { color: var(--fp-ink); }
  .fp-fewbtn:focus-visible { outline: 2px solid var(--fp-accent); outline-offset: 2px; }

  /* ── notices ── */
  .fp-note {
    margin: 0 16px 14px; padding: 10px 12px; border-radius: 10px;
    background: var(--fp-warn-bg); color: var(--fp-warn-ink); font-size: 12px;
    border: 1px solid rgba(255, 176, 32, .22);
  }
  .fp-empty { padding: 4px 16px 16px; color: var(--fp-soft); font-size: 13px; }
  .fp-quota {
    padding: 0 16px 6px; font-size: 17px; font-weight: 600;
    color: var(--fp-ink); font-family: var(--fp-mono);
    font-variant-numeric: tabular-nums; letter-spacing: -.02em;
  }
  .fp-ft {
    padding: 11px 16px 13px; font-size: 11px; color: var(--fp-faint);
    border-top: 1px solid var(--fp-line-2);
    font-variant-numeric: tabular-nums;
  }
  .fp-skel { padding: 16px; color: var(--fp-faint); font-size: 13px; }

  /* Under ~520px a six-column table stops being a table and starts being a
     horizontal scroll nobody scrolls, so each row becomes a block. */
  @media (max-width: 519px) {
    .fp-t thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .fp-t, .fp-t tbody, .fp-t tr, .fp-t td { display: block; width: 100%; }
    .fp-t tr { padding: 8px 0; border-bottom: 1px solid var(--fp-line-2); }
    .fp-t tr:last-child { border-bottom: 0; }
    .fp-t td { border: 0; padding: 2px 16px; }
    .fp-t tr[data-best="1"] td:first-child { box-shadow: inset 3px 0 0 var(--fp-accent); }
    .fp-t td.fp-right { text-align: left; padding-top: 8px; padding-bottom: 6px; }
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

  /* ── host bridge (see the docstring) ──
     Three hosts, one render path, converging on renderOnce. */
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
  /* The host's theme is deliberately IGNORED: one look everywhere. */
  request("ui/initialize", {
    appInfo: { name: "flightpowers-flights-card", version: "1.0.0" },
    appCapabilities: {},
    protocolVersion: "2026-01-26"
  }, function () { sendInitialized(); });
  if (document.readyState === "complete") setTimeout(sendInitialized, 400);
  else window.addEventListener("load", function () { setTimeout(sendInitialized, 400); });
  setTimeout(sendInitialized, 900);

  /* ── what may be opened ──
     An ALLOWLIST, not a scheme check; see "Links" in the docstring. */
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

  /* ── reading the payload ──
     Nothing below assumes a field exists; a missing one drops its
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
  /* price_insights_* are bare numbers while `price` is "$231", so the
     symbol is read off the row; no price string -> no prefix. */
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

  /* ── the price band ──
     Google's own tracking, drawn only when both edges are there. */
  function bandOf(rows, cheapest, cheapestRow, routes) {
    /* Google tracks each route separately, so the band is the CHEAPEST
       route's own and RESTRICTED to it: no insights there, no band. */
    var pool = rows, dest = cheapestRow ? destOf(cheapestRow) : "";
    if (dest) {
      pool = rows.filter(function (r) { return destOf(r) === dest; });
      pool.unshift(cheapestRow);
    } else if (routes > 1) {
      /* Unnamed cheapest fare with other routes present: every band on
         offer belongs to one of THEM, so none is drawn. */
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

    /* Several routes: say out loud that this band is one of them, the
       cheapest, rather than letting it read as if it covered all. */
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
     Grouped on the RAW `to_airport`; only the LABEL is shortened. */
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
  /* One bucket per row, first-seen order, unlabelled last: `dest: ""`
     IS the Other bucket, so nothing downstream special-cases it. */
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
  /* The label is capped, so the full upstream value lives on the
     cell's title rather than being lost. */
  function destCell(r) {
    var raw = destOf(r);
    var td = cell("To", raw ? destLabel(raw) : "", "", "fp-dest");
    if (raw) td.setAttribute("title", raw);
    return td;
  }
  function renderTable(rows, rt, cheapest, showDest) {
    /* Only the FIRST row at the cheapest price is badged: a date range
       returns identical fares, and badging both says it twice. */
    var badged = false;
    /* Only while the table holds more than one destination. */
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
    rows.forEach(function (r) {
      var tr = el("tr");
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
    return scroll;
  }

  /* ── how many rows are on screen ──
     Five, plus a button for five more. See the docstring for why. */
  var PAGE_ROWS = 5;
  /* What is on screen of what was found, the next page with how many
     are left, and past page one the way back. */
  function moreLine(shown, total, onMore, onFewer) {
    var wrap = el("div", "fp-more");
    wrap.appendChild(el("span", "fp-more-t", "Showing " + shown + " of " + total));
    var acts = el("div", "fp-more-acts");
    var left = total - shown;
    if (left > 0) {
      var step = Math.min(PAGE_ROWS, left);
      /* "Show 5 more · 274 left"; the tail is dropped on the last page,
         where "Show 3 more · 3 left" says the same thing twice. */
      var b = el("button", "fp-morebtn",
        "Show " + step + " more" + (left > step ? " · " + left + " left" : ""));
      b.type = "button";
      b.addEventListener("click", onMore);
      acts.appendChild(b);
    }
    if (shown > PAGE_ROWS) {
      var f = el("button", "fp-fewbtn", "Show fewer");
      f.type = "button";
      f.addEventListener("click", onFewer);
      acts.appendChild(f);
    }
    wrap.appendChild(acts);
    return wrap;
  }

  /* ── header + footer ── */
  /* Whose answer this is, before what the answer is. The mark is
     decorative -- the wordmark carries the name -- so it is aria-hidden. */
  function brandRow() {
    var wrap = el("div", "fp-brand");
    var mark = el("span", "fp-logo");
    mark.setAttribute("aria-hidden", "true");
    wrap.appendChild(mark);
    wrap.appendChild(el("span", "fp-word", "FlightPowers"));
    return wrap;
  }
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

    root.appendChild(brandRow());
    root.appendChild(headerOf(sc, rows));

    /* The non-search exits are data a model has to relay, so the card
       relays them too instead of drawing an empty table. */
    /* A refusal: two numbers, then the message. See the docstring. */
    if (status === "quota_exceeded") {
      var need = num(sc.combos_requested), got = num(sc.combos_allowed_now);
      if (need !== null && got !== null) {
        root.appendChild(el("div", "fp-quota",
          need.toLocaleString("en-US") + " requests needed · "
          + got.toLocaleString("en-US") + " left"));
      }
    }
    var msg = txt(sc.message) || txt(sc.partial);
    if (sc.needs_api_key === true || sc.quota_exhausted === true
        || status === "degraded" || status === "trial_exhausted"
        || status === "quota_exceeded" || status === "empty" || msg) {
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

    /* `selected` is the pills' current rows; band, marker and counts are
       recomputed from it because Google's band is per route. */
    var body = el("div", "fp-body");
    var selected = rows;
    /* How many of `selected` are drawn. Reset to one page whenever the
       selection changes: a pill is a new question. */
    var shownCount = PAGE_ROWS;

    function paint() {
      while (body.firstChild) body.removeChild(body.firstChild);
      var sym = symbolOf(selected);
      var cheapest = null, cheapestRow = null;
      selected.forEach(function (r) {
        var n = priceNumber(r);
        if (n !== null && (cheapest === null || n < cheapest)) { cheapest = n; cheapestRow = r; }
      });
      /* Routes in the table RIGHT NOW: it decides the To column and what
         the band may claim. */
      var routes = bucketsOf(selected).length;
      /* Computed from the whole SELECTION, never from the page on screen:
         "cheapest" must not change when someone taps Show more. */
      var band = bandOf(selected, cheapest, cheapestRow, routes);
      if (band) body.appendChild(renderBand(band, sym, routes));
      var total = selected.length;
      var shown = Math.min(Math.max(PAGE_ROWS, shownCount), total);
      body.appendChild(
        renderTable(selected.slice(0, shown), isRoundTrip(selected), cheapest, routes > 1));
      if (total > PAGE_ROWS) {
        body.appendChild(moreLine(shown, total,
          function () { shownCount = shown + PAGE_ROWS; paint(); },
          function () { shownCount = PAGE_ROWS; paint(); }));
      }
      sizeChanged();
    }

    var buckets = bucketsOf(rows);
    if (buckets.length > 1) {
      root.appendChild(renderPills(buckets, rows, function (picked) {
        selected = picked;
        shownCount = PAGE_ROWS;
        paint();
      }));
    }
    root.appendChild(body);
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
    /* Gate on source, never origin: sandbox proxies vary the origin, the
       sender window does not. A NULL source is refused too. */
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


#: The FlightPowers robot mark, the same file the site serves at
#: `public/brand/robot-mark-56.png` (2,737 bytes), inlined because this
#: frame is allowed no network of any kind. Read at import from a module
#: constant rather than from disk: the deployment bundle is the source of
#: truth for what is served, and a file read at request time on a cold
#: serverless instance is a failure mode this card does not need.
MARK_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAC8AAAA4CAMAAABaKlG9AAADAFBMVEVMaXETH1AtN6Yp"
    "SLkNFzwmKlYKECpPbM8XJoYKFDUNFzkzRaxXedcfMYM7a98qSbgwO7IsTsMVHT0KESol"
    "MY4fMYUgN5A6ctkNFTYMFTUOFzslQaweK25AQrMkMZgwUbU/dM5CTbYuL44vVMpGjNoZ"
    "IVcuRbwZJ2gWJmRVcrAvNpIfKnwoLYgrUa0MFTMOEi89PKEiNItHjepIQbQpPaQmOqAm"
    "MYEiLHwrMaBAOrA4P488YtMkPZ03TLI8et40NZ0OGDsSFDEOFzwWHkkNEzBVRLcLEy5F"
    "QbUKEClSSblGPblbSbdDPrcKECYzPKkoRag1N5AZLW5LldYpLIRAcNsvV7g/UddHlOc0"
    "ZbwKEC1Fi+I2ZtQbIlcADBkkLX0aJmkoRLQ4TcE/ddQ/fd89c903Y9lNQ7pAOag2NJVD"
    "gdJDXtNDiuc8WskxNZ5AgtwXKWY2ZcZSiuswWbsqTa4yW7oKEiwgGEMOEShOQbYUIEoM"
    "Ei4aFjkKECkjNI4PGDRDWdhOl9wvUsM2MIMyWMthp/WP+v8NFjUsUa05adkNFzoOGDsL"
    "EiwKESkvUMgMEzAwVcsNFTMzV9A5aNwrSLoyUMwOFzcKDyUSHUgIFT82Ydk/dOU1X9Yj"
    "N5g3YN0OGT80XNMqRbYrS74uVdIxWtgpQKw3ZNopQ7I9cOPY//8LFTkuTsIxSsUsS8Ic"
    "KnIJGUk5Y+A8beMVIlY6aOLE7/onPqUlOZ0QIVEvVMUUGD0WI18pO6ozWtIqSbErM6BB"
    "eu83YtM1YssbMXUlO6E5PbpAeugcMnw5adcvP7c2a9E1OawyWs1KddpVgd4xR8IuR78i"
    "NJIoMZUfMIo/cespOKUtUL0aL2oZKGYvWLY1VIYjP5q86PbR+v/L+P4tNqk9d+IhOYwj"
    "PXIsSH2t2PLM8PuDsOo4bdsnRqA8YOU7c96ApsUlRJCz3vVJbJp8qedfjONCa/hGiPgl"
    "TtOFrc1WeaQnTMul0/SWxfB0mLlliLNQcJ93zP9bfqdkqv5rj7OSt9JllOOKNdaLAAAA"
    "znRSTlMAof7QRwN0AgEYdBgG/v7+/vgKR/51ov6hzvjQGyv+Sich/flBKf1yRxBI/v7+"
    "92lzzu+tpPqIpxP+VrvXNv39oTTQ291doMPMTOs9+/rA+zqjMOqQdkCa/Ty2zlsU1ev4"
    "/XSf4PyV39hie/tT6Nz4pubF8/KmpliMfqmJfPVS71HnY+D+/vzt9P//////////////"
    "////////////////////////////////////////////////////////////////////"
    "/////////iUO0/8AAAAJcEhZcwAACxIAAAsSAdLdfvwAAAZ9SURBVEjHlZV3XBRXEMef"
    "F9e7CHcHBEEjJRDpYO89amyJGmOJppree+89+dzu3e5e73uVInDAcZTQe0dBOoK9JbZE"
    "03vmqcRAwJDfP2937zvzZt6bmUNoWG2duZVAAjQ68VHwJ5/+th3W0UmA5r/4y4tho+YB"
    "3G5+ZvQ4kHe2bf9f/JNfPzX68Pne6OGvFyFvvmBUNOQb88ip93G+/P/cA3x6T3nohWTz"
    "qVnT18Vg6/+IPOahR7IyA7PNpx48ULX+Da9rZ8FHU54rzc4sC8zOzHxwTp1P1fpbr2UA"
    "57K0TSTKLgferMpR7fHxWTAdESPjHzz9pSE5K6u2NjPRnO9Upe3x4S0YcQc+umnmwcBq"
    "MKitzzabU5XAR/JK1gePYMBHd30TuLTaYEhWlGfq9R0yVVraHl5J1azhCxWK4OlqQyDm"
    "s8qMZnmqXCVL28vjHbj9tuE2EHiDe4Mh8CDmFY3GfDnmI50HeFWPIe9/70DwQ58rNZQa"
    "IOFkkcjIyIvkEI+1zofX8PjCYXh0V/1SQ31Cd32pQaRI1BsdchXIafJvP7Bg2ayhzUag"
    "haU7lio6NWr36fKsrESjRQ7xFAu1tKagfUG4z0eDbwGMX0recTCBUnM6XUbnJovZ7JD1"
    "duno3SRN7WsIL3nCe9AGfBT2SvKOL3ulXMbZiz06WltxqEJL6Xou/pzHSV0N4XXLwgZt"
    "AFf1iqj7xCFKd2R/yv4eHSvVaGj87OnRXeaXDDpT4F9uTDhxmtKdTfGkfJXBcSTJZfye"
    "cizlVx3laogvCQ8e5B+KfrHy5ImTtO5s37GUP/JIyW4JmfEV8N/qWCEvLmRLzOADItBc"
    "cW95Byc5vL8P/JMSCfA/pvR5juhIU6uwdd6QKiVQ0OLcBHMFlfGt50yP7hKvO3K0/6c8"
    "uqswPm5LxNCqJlDonJNFTAWZcfgwdr8bB9R5OI/syi2Jj1/47woi0JKAmxlLDQueIXrg"
    "8cq6CkOWzbtjuIKDWfCCXVYsIXEwIGxASkwhJbNH6HoChX2s8nWzkr9Fsu72kGfDRupI"
    "PlryoV1IceQAz9E1hc/eca0GDn4piWO5yyGRJM21bxyhG/l8gYBPQNIztBpaQnJww2pa"
    "o70/FhEEISAI/tCpNlAWWjUlldIsy8IiVXP+A+H8M2cYmVO2Llq0aBvaVvu8Rq2mMSuV"
    "UlJNAW8umr5x45rH1nlfjQs6/e6DbdmZjHDz56LyCrX6soVGIy0whWwZ84Czrq6qas3f"
    "XQ9DcGapQvGo5dBnvScb9cYatxZHz3LagkrTvjghHbcnLdLH5/YrgwuPqWrcsvluLr88"
    "IZ+pr83vKC4uFtuSWlr2xZlMnBZPrl3tV+aKAN1dCrjRWEx2nUiu7e6uZ+x2RiZLVzpb"
    "hCbr3kK3OkeVvjeSx1uDux6iqVYo9EZLUa/0dJtCJCprzO/osNnEVqtVLFOlKq0u6SFZ"
    "+l5rYUjDOvwXgh5ue1RvKZIzNVR3lkKhSITQyiyNjXZLrUMuA36f1AV4UntrySwckPc9"
    "ZotFzjCpBWQ5xhP1eofDoXc0HT1+4RJvYt1KZVJhrn/r4zG4zO4xMwyjVFq1eSJ9IsjR"
    "fO58s6LZ42mCNNKVSTmc1umszGnxb33CDx9PqC+4EStzyE5IWq/HnvuOnfF4mu1yRmYT"
    "JxVq1SZnbq6/f/hCfNcEihaK0205BZrO/CKLxWg0OvTnj/b3HXdYwI/N6jS5NW5TZUu8"
    "695LpUGg92A+CfMoTU1Hh7wITCx2x/f9/T/Yi1Q4nFxTl0aqdRVoqdev8GO0G6QUxUpr"
    "YHdGLrc3Hf9h//nvz5TZsfvK3FyXhqagltSvYV6A/FZqoXZpmqpQptuKZQxTdq7/zwvN"
    "nqbvZDZlZU5liwu8UTSrXukHNIEi8jiod5be8HySMl0sBoPvjjfb7eea0lNtzhyY6A9s"
    "AFjN0l/cAjSB7s27ETSVXRtl2iUWb/L19b0ZC9bIXXM2zXlXGLWWhd+nTv1i2iX+zZWT"
    "VqxYMWnVO/NnLJ583eTJ1w0IXhZPnjHj/qBpqyaB1q56GyfwzOaJy5cvn7j6rZ1obkDA"
    "7NkB88YMaHPA7ICAeXPRLa9OBK1ePSYWIa+ntkWNB42b5ociJkyYEDV+3FWNj4IvEchv"
    "2jhMjJ8w3QvdFBoaGj127A037IS9gqLhYexVwUt0BBz5TvwxOjY2NhjN98K6HoRP677r"
    "h+o+fOL4wcsrKCho/l/lRgz0CMRqDgAAAABJRU5ErkJggg=="
)

#: What every host actually receives: the document above with the brand
#: mark's bytes substituted in, and nothing else done to it.
#:
#: There WAS a build step here -- the frame was authored with 12 KB of
#: comments and served with them stripped, to pay for the inlined mark.
#: A zero-context review found two documents it corrupted: an apostrophe
#: in HTML prose ("Google's band") desynchronised the quote state and ate
#: the JavaScript after it, and a `//` line comment containing `/*` opened
#: a block comment that swallowed the next string. Both are fixable and
#: neither is worth fixing: a stripper correct on every input is a
#: JavaScript tokenizer, and this is a 35 KB string constant. So the
#: comments moved out of the frame instead -- the long ones into the
#: docstring above, the rest shortened -- and the served bytes are now the
#: authored bytes. `tests/test_widget.py` runs `node --check` over them.
FLIGHTS_WIDGET_HTML = _FRAME_SOURCE.replace("__FP_MARK_B64__", MARK_PNG_BASE64)
