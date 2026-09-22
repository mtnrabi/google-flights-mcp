"""The widget's JavaScript, actually executed, against a hostile payload.

Why this file exists
--------------------
Every other assertion about the frame in `tests/test_widget.py` is made on
the served BYTES: the token is there, the guard is there. That is worth
having and it is not the same thing as the code behaving. The two rules that
matter here are both runtime rules:

* **Upstream strings are data, never markup.** Airline names, city names and
  the price verdict come from Google by way of our backend. They are written
  with `textContent`, so a `<script>` in an airline name must end up as four
  visible characters and nothing else.
* **A Book button only ever opens a booking URL we emit.** `buy_link` is
  upstream data too, and the host's link opener is the one place this frame
  could do real harm, so the gate is an allowlist: https, and
  google.com / booking.com / stay22.com or a subdomain. Everything else --
  `javascript:`, plain http, another host, a `google.com.evil.test`
  lookalike -- renders the price with no button at all.

and one transport rule:

* **A message with no source is not the host.** The tool result decides
  what this frame renders and what its buttons open.

How it runs
-----------
Headless Chrome with `--virtual-time-budget`, which fast-forwards the
frame's own timers and then dumps the DOM, so no browser-automation
dependency is added to this package. The page under test is the real
`FLIGHTS_WIDGET_HTML` with a probe script appended; the probe writes its
findings into the document title as base64 JSON, which is the one thing that
survives `--dump-dom` unambiguously.

SKIPPED when no Chrome binary is on the machine (CI), which is why every
rule above is ALSO pinned as a bytes assertion in `tests/test_widget.py`.
This file is the one that proves them.
"""

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

from src.widget import FLIGHTS_WIDGET_HTML

CHROME_CANDIDATES = (
    os.environ.get("CHROME") or "",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def _chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        found = candidate if os.path.isfile(candidate) else shutil.which(candidate)
        if found:
            return found
    cached = sorted(
        pathlib.Path.home().glob(
            ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"
        )
    )
    return str(cached[-1]) if cached else None


#: Four kinds of poison in one response: markup in three different string
#: fields, and five links of which exactly ONE is allowed.
HOSTILE = {
    "search_status": "ok",
    "result_count": 5,
    "search_coverage": {
        "destinations_searched": ["<script>window.pwnEyebrow=1</script>BUD"],
        "departure_dates_searched": ["2026-11-10"],
    },
    "api_usage": {"requests_used_by_this_call": 1, "plan_requests_remaining": 10},
    "results": [
        {
            "price": "$1",
            "price_as_number": 1,
            "airline": "<img src=x onerror=window.pwnAirline=1>Air",
            "stops": 0,
            "duration": "1 hr",
            "departure_description": "<b>bold</b> 10:00",
            "from_airport": "TLV",
            "to_airport": "BUD",
            "buy_link": "javascript:window.pwnLink=1",
            "price_insights_low": 10,
            "price_insights_high": 20,
            "price_range_in_relation_to_other_periods": "<script>window.pwnVerdict=1</script>",
        },
        # Plain http, on an allowed host: still refused. Handing a user a
        # downgraded booking link is not something to do quietly.
        {"price": "$2", "price_as_number": 2, "airline": "B", "stops": 1,
         "duration": "2 hr", "departure_description": "ok",
         "buy_link": "http://www.google.com/travel/flights/x"},
        # Right string in the path, wrong host.
        {"price": "$3", "price_as_number": 3, "airline": "C", "stops": 0,
         "duration": "3 hr", "departure_description": "ok",
         "buy_link": "https://evil.test/google.com/travel"},
        # The lookalike the suffix match has to refuse.
        {"price": "$4", "price_as_number": 4, "airline": "D", "stops": 0,
         "duration": "4 hr", "departure_description": "ok",
         "buy_link": "https://google.com.evil.test/travel"},
        # The only good one.
        {"price": "$5", "price_as_number": 5, "airline": "E", "stops": 0,
         "duration": "5 hr", "departure_description": "ok",
         "buy_link": "https://www.google.com/travel/flights/ok"},
    ],
}

BENIGN = {
    "search_status": "ok",
    "results": [{"price": "$9", "price_as_number": 9, "airline": "Z", "stops": 0,
                 "duration": "1 hr", "departure_description": "ok",
                 "buy_link": "https://www.google.com/travel/flights/z"}],
}

PROBE = """
<script>
(function () {
  var out = {};
  function done() {
    /* btoa only takes Latin-1 and the card renders an em dash and an
       arrow, so the JSON goes through UTF-8 percent-encoding first and
       comes back out as utf-8 on the Python side. */
    try {
      var json = JSON.stringify(out);
      document.title = "RESULT:" + btoa(unescape(encodeURIComponent(json)));
    } catch (e) { document.title = "RESULTERROR:" + String(e); }
  }
  var scriptsBefore = document.querySelectorAll("script").length;

  /* 1. A message with NO source, carrying a perfectly good payload. The
        frame must ignore it -- if it renders, the guard is not a guard. */
  window.dispatchEvent(new MessageEvent("message", {
    data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
            params: { structuredContent: __BENIGN__ } },
    source: null
  }));

  setTimeout(function () {
    out.renderedFromNullSource = !!document.querySelector("table.fp-t");

    /* 2. The hostile payload, delivered the way the host delivers one. */
    window.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                         params: { structuredContent: __HOSTILE__ } }, "*");

    setTimeout(function () {
      var rows = document.querySelectorAll("table.fp-t tbody tr");
      out.rows = rows.length;
      out.buttons = document.querySelectorAll("button.fp-book").length;
      out.buttonRows = [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].querySelector("button.fp-book")) {
          out.buttonRows.push(rows[i].textContent.replace(/\\s+/g, " ").trim());
        }
      }
      out.scriptsAdded = document.querySelectorAll("script").length - scriptsBefore;
      out.imgs = document.querySelectorAll("img").length;
      out.boldTags = document.querySelectorAll("table.fp-t b").length;
      out.pwn = [window.pwnEyebrow, window.pwnAirline, window.pwnLink,
                 window.pwnVerdict].filter(function (v) { return v; }).length;
      out.bodyText = document.body.innerText;
      /* 3. The refused links must not even reach the host: click every
            button that exists and record what openLink actually posted. */
      out.posted = [];
      var realPost = window.parent.postMessage;
      window.parent.postMessage = function (msg) {
        if (msg && msg.method === "ui/open-link") out.posted.push(msg.params.url);
      };
      var all = document.querySelectorAll("button.fp-book");
      for (var j = 0; j < all.length; j++) all[j].click();
      window.parent.postMessage = realPost;
      done();
    }, 50);
  }, 50);
})();
</script>
"""


def _inline(payload: dict) -> str:
    """JSON safe to paste inside a `<script>`.

    The hostile payload contains the literal string `</script>`, and an
    HTML parser ends the script block at it whatever the JavaScript
    context -- which silently broke this whole probe the first time it
    ran. Escaping the slash keeps the JSON valid and the block intact.
    """
    return json.dumps(payload).replace("</", "<\\/")


def _run_page(
    chrome: str, probe: str, width: int = 800, light: bool = False
) -> dict:
    """The real frame plus `probe`, executed, its findings decoded.

    `width` is pinned per call: the card has three layouts (block rows
    under 520px, a wrapping table under 640, unwrapped above it) and how
    many rows fit in the height budget depends on which one is in force,
    so a test that did not state its width would be asserting against
    whatever Chrome's default window happens to be.
    """
    page = FLIGHTS_WIDGET_HTML + probe
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "widget.html"
        path.write_text(page, encoding="utf-8")
        proc = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--virtual-time-budget=6000",
                *(
                    ["--force-prefers-color-scheme=light"]
                    if light
                    else []
                ),
                f"--user-data-dir={tmp}/profile",
                f"--window-size={width},1400",
                "--dump-dom",
                path.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    # Scoped to the <title> ELEMENT, not the whole dump: the dump also
    # contains the probe's own source, in which both marker strings appear
    # as literals. Matching loosely reported a thrown probe on a clean run.
    title = re.search(r"<title>(.*?)</title>", proc.stdout, re.S)
    text = (title.group(1) if title else "").strip()
    if text.startswith("RESULTERROR:"):
        raise AssertionError("probe threw: " + text[len("RESULTERROR:"):])
    match = re.match(r"RESULT:([A-Za-z0-9+/=]+)$", text)
    assert match, (
        f"probe did not report; <title> was {text[:120]!r}\nstderr tail:\n"
        + proc.stderr[-1000:]
    )
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


def _run(chrome: str) -> dict:
    return _run_page(
        chrome,
        PROBE.replace("__HOSTILE__", _inline(HOSTILE)).replace(
            "__BENIGN__", _inline(BENIGN)
        ),
    )


def _chrome_or_skip() -> str:
    chrome = _chrome()
    if not chrome:
        pytest.skip(
            "no Chrome binary on this machine, so the widget's JS cannot be "
            "executed here; the same rules are pinned as bytes assertions in "
            "tests/test_widget.py"
        )
    return chrome


@pytest.fixture(scope="module")
def rendered():
    return _run(_chrome_or_skip())


class TestTheWidgetUnderAHostilePayload:
    def test_a_message_with_no_source_renders_nothing(self, rendered):
        assert rendered["renderedFromNullSource"] is False

    def test_markup_in_upstream_strings_stays_text(self, rendered):
        assert rendered["scriptsAdded"] == 0, "a <script> in the payload became one"
        assert rendered["imgs"] == 0, "an <img onerror> in the payload became one"
        assert rendered["boldTags"] == 0, "a <b> in a cell became markup"
        assert rendered["pwn"] == 0, "payload script executed"
        # ...and it is still visible to the user, as characters.
        assert "<img src=x onerror=window.pwnAirline=1>Air" in rendered["bodyText"]

    def test_only_the_one_allowed_link_gets_a_button(self, rendered):
        assert rendered["rows"] == 5
        assert rendered["buttons"] == 1, (
            "expected exactly one Book button: javascript:, plain http on an "
            "allowed host, a wrong host and a google.com.evil.test lookalike "
            "must all render the price with no button"
        )
        assert "$5" in rendered["buttonRows"][0]

    def test_no_refused_url_ever_reaches_the_host(self, rendered):
        assert rendered["posted"] == [
            "https://www.google.com/travel/flights/ok"
        ]


# ── the top five, the Show-more button, and the destination pills ───────
#
# Matan, 2026-09-22, watching a demo clip scroll through a 93-row list:
# "wtf is that long scroll. limit the presented items to the top 10, and
# allow the user to tap on a button to view more. also - what about
# multiple destinations support?" Then, on the ten-row card: "make it top
# 5, still seems too long."
#
# v2 had answered "showcase the top 10, scrollable for more" by capping the
# table's HEIGHT and letting the reader scroll inside it. At 93 and 279 rows
# that is a scroll trap inside a chat message, which is what he was looking
# at. v3 draws five ROWS, appends five per tap, and has no scroll container
# and no height ceiling at all -- the card is as tall as what it draws and
# the HOST scrolls the page.
#
# All of that is layout behaviour, which is exactly the kind of claim a
# bytes assertion cannot make: "the code slices the array" is not "five rows
# are in the DOM, the sixth is not, and nothing scrolls inside the box".
# So it is measured here, in a real engine, at a stated width.

_TIMES = [
    "6:05 AM", "7:40 AM", "9:15 AM", "10:50 AM", "12:25 PM", "1:35 PM",
    "3:10 PM", "4:45 PM", "6:20 PM", "8:00 PM", "9:30 PM", "11:05 PM",
]
#: One decade of prices per destination, so a filtered table can be checked
#: by reading its prices: every Athens fare is $2xx and nothing else is.
_DESTS = [
    ("Rome (FCO)", 100, "ITA Airways", 118, 240),
    ("Athens (ATH)", 200, "Aegean", 205, 330),
    ("Budapest (BUD)", 300, "Wizz Air", 312, 460),
]


def _rows() -> list[dict]:
    out: list[dict] = []
    for dest, base, airline, low, high in _DESTS:
        for i, time in enumerate(_TIMES):
            price = base + i * 9
            out.append(
                {
                    "price": f"${price}",
                    "price_as_number": price,
                    "airline": airline if i % 3 else "Ryanair",
                    "stops": 0 if i % 2 else 1,
                    "duration": f"{3 + i % 3} hr {(i * 7) % 60} min",
                    "departure_description": f"{time} on Tue, Nov 10",
                    "arrival_description": f"{_TIMES[(i + 4) % 12]} on Tue, Nov 10",
                    "from_airport": "Tel Aviv (TLV)",
                    "to_airport": dest,
                    "departure_date": "2026-11-10",
                    "price_insights_low": low,
                    "price_insights_high": high,
                    "price_range_in_relation_to_other_periods": "typical",
                    "buy_link": f"https://www.google.com/travel/flights?tfs={dest[:3]}{i}",
                }
            )
    out.sort(key=lambda r: r["price_as_number"])
    return out


#: Three destinations x twelve fares: the shape "TLV to Rome, Athens or
#: Budapest on the 10th" actually returns.
MULTI = {
    "search_status": "ok",
    "result_count": 36,
    "search_coverage": {
        "destinations_searched": ["Athens (ATH)", "Budapest (BUD)", "Rome (FCO)"],
        "departure_dates_searched": ["2026-11-10"],
    },
    "api_usage": {"requests_used_by_this_call": 3, "plan_requests_remaining": 46150},
    "results": _rows(),
}

#: The same shape with one destination and three fares: no pills, no
#: scroll, no footer count -- the card it was before this change.
SINGLE = {
    "search_status": "ok",
    "result_count": 3,
    "search_coverage": {
        "destinations_searched": ["Budapest (BUD)"],
        "departure_dates_searched": ["2026-11-10"],
    },
    "api_usage": {"requests_used_by_this_call": 1, "plan_requests_remaining": 46152},
    "results": [r for r in _rows() if r["to_airport"] == "Budapest (BUD)"][:3],
}

LAYOUT_PROBE = r"""
<script>
(function () {
  var out = {};
  function done() {
    try { document.title = "RESULT:" + btoa(unescape(encodeURIComponent(JSON.stringify(out)))); }
    catch (e) { document.title = "RESULTERROR:" + String(e); }
  }
  function box() { return document.querySelector(".fp-scroll"); }
  function rows() { return document.querySelectorAll("table.fp-t tbody tr"); }
  function moreBtn() { return document.querySelector(".fp-morebtn"); }
  function fewBtn() { return document.querySelector(".fp-fewbtn"); }
  /* Everything that can change when a button is tapped, in one object.
     `vscroll` is the assertion that matters most in v3: the card must
     have NO inner vertical scroll at all -- the host scrolls the page. */
  function snap() {
    var b = box(), card = document.querySelector(".fp-card");
    var m = document.querySelector(".fp-more");
    return {
      rows: rows().length,
      more: m ? m.textContent.replace(/\s+/g, " ").trim() : null,
      moreText: m ? m.querySelector(".fp-more-t").textContent.trim() : null,
      moreBtn: moreBtn() ? moreBtn().textContent.trim() : null,
      fewBtn: fewBtn() ? fewBtn().textContent.trim() : null,
      vscroll: b ? b.scrollHeight - b.clientHeight : 0,
      hscroll: b ? b.scrollWidth - b.clientWidth : 0,
      pageScroll: document.documentElement.scrollWidth
                  - document.documentElement.clientWidth,
      cardHeight: Math.round(card.getBoundingClientRect().height)
    };
  }
  function prices() {
    var rs = rows(), o = [];
    for (var i = 0; i < rs.length; i++) o.push(rs[i].querySelector("td.fp-num").textContent.trim());
    return o;
  }
  function pills() {
    var ps = document.querySelectorAll("button.fp-pill"), o = [];
    for (var i = 0; i < ps.length; i++) {
      o.push({ text: ps[i].textContent.trim(),
               pressed: ps[i].getAttribute("aria-pressed") });
    }
    return o;
  }
  function band() {
    var b = document.querySelector(".fp-bar");
    return b ? b.getAttribute("aria-label") : null;
  }
  function bandLabel() {
    var t = document.querySelector(".fp-band-top span");
    return t ? t.textContent.trim() : null;
  }
  function bandLine() {
    var t = document.querySelector(".fp-bandline");
    return t ? t.textContent.replace(/\s+/g, " ").trim() : null;
  }
  function destTitles() {
    var cs = document.querySelectorAll("table.fp-t tbody td.fp-dest"), o = [];
    for (var i = 0; i < cs.length; i++) o.push(cs[i].getAttribute("title"));
    return o;
  }
  function headers() {
    var hs = document.querySelectorAll("table.fp-t thead th"), o = [];
    for (var i = 0; i < hs.length; i++) o.push(hs[i].textContent.trim());
    return o;
  }
  function dests() {
    var cs = document.querySelectorAll("table.fp-t tbody td.fp-dest"), o = [];
    for (var i = 0; i < cs.length; i++) o.push(cs[i].textContent.trim());
    return o;
  }
  function badged() {
    var t = document.querySelector("tr[data-best='1'] td.fp-num");
    return t ? t.textContent.trim() : null;
  }
  function pill(i) { return document.querySelectorAll("button.fp-pill")[i]; }

  window.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                       params: { structuredContent: __PAYLOAD__ } }, "*");

  /* A queue rather than nested callbacks: v3 is a sequence of taps and
     each one needs a turn of the event loop to repaint. */
  var steps = [
    function () {
      out.width = window.innerWidth;
      out.pills = pills();
      out.s0 = snap();
      out.prices = prices();
      out.band = band();
      out.bandLabel = bandLabel();
      out.bandLine = bandLine();
      out.bandBar = !!document.querySelector(".fp-bar");
      out.destTitles = destTitles();
      out.headers = headers();
      out.dests = dests();
      out.badged = badged();
      /* The look, read off the COMPUTED styles: "the CSS says #0c0e11" is
         not the same claim as "the card is painted #0c0e11 on this host". */
      var card = document.querySelector(".fp-card");
      var cs = getComputedStyle(card);
      out.cardBg = cs.backgroundColor;
      out.bodyColor = getComputedStyle(document.body).color;
      out.colorScheme = getComputedStyle(document.documentElement).colorScheme;
      var logo = document.querySelector(".fp-logo");
      out.logoBg = logo ? getComputedStyle(logo).backgroundImage.slice(0, 40) : null;
      out.logoSize = logo
        ? Math.round(logo.getBoundingClientRect().width) + "x"
          + Math.round(logo.getBoundingClientRect().height)
        : null;
      out.wordmark = document.querySelector(".fp-word")
        ? document.querySelector(".fp-word").textContent.trim() : null;
      var best = document.querySelector("tr[data-best='1'] td.fp-num");
      out.bestPriceColor = best ? getComputedStyle(best).color : null;
      out.bestPriceSize = best ? getComputedStyle(best).fontSize : null;
      out.bestPriceFont = best ? getComputedStyle(best).fontFamily.slice(0, 13) : null;
      var plain = document.querySelectorAll("tr:not([data-best]) td.fp-num")[0];
      out.plainPriceColor = plain ? getComputedStyle(plain).color : null;
      var sel = document.querySelector("button.fp-pill[aria-pressed='true']");
      out.selectedPillBg = sel ? getComputedStyle(sel).backgroundColor : null;
      var gauge = document.querySelector(".fp-bar .fp-mark");
      out.gaugeMarker = gauge ? getComputedStyle(gauge).backgroundColor : null;
      out.gaugeGlow = gauge ? getComputedStyle(gauge).boxShadow.indexOf("rgb(255, 176, 32)") : -2;
      var track = document.querySelector(".fp-bar");
      out.gaugeHeight = track ? Math.round(track.getBoundingClientRect().height) : null;
      var mb = document.querySelector(".fp-morebtn");
      out.moreBtnWidth = mb
        ? Math.round(mb.getBoundingClientRect().width / card.getBoundingClientRect().width * 100)
        : null;
      out.moreBtnColor = mb ? getComputedStyle(mb).color : null;
      var tag = document.querySelector(".fp-bestpill");
      out.tags = tag ? tag.textContent.trim() : null;
      out.tagBelow = tag
        ? getComputedStyle(tag).display === "block"
          && tag.getBoundingClientRect().top
             > best.getBoundingClientRect().top + 8
        : null;
    },
    /* Tap once: the next ten are appended, nothing is replaced. */
    function () { if (moreBtn()) moreBtn().click(); },
    function () { out.s1 = snap(); out.s1_dests = dests(); },
    /* Twice. */
    function () { if (moreBtn()) moreBtn().click(); },
    function () { out.s2 = snap(); },
    /* And back to one page. */
    function () { if (fewBtn()) fewBtn().click(); },
    function () { out.s3 = snap(); },
    /* A pill: a new selection, and the count resets to one page. */
    function () { if (pills().length > 2) pill(2).click(); },
    function () {
      if (pills().length <= 2) return;
      out.f_pressed = pills().map(function (p) { return p.pressed; });
      out.f_prices = prices();
      out.f_band = band();
      out.f_bandLabel = bandLabel();
      out.f_bandLine = bandLine();
      out.f_headers = headers();
      out.f_dests = dests();
      out.f_badged = badged();
      out.fs = snap();
    },
    function () { if (pills().length > 2 && moreBtn()) moreBtn().click(); },
    function () { if (pills().length > 2) out.fs1 = snap(); },
    /* All puts the whole answer back -- at one page, not at whatever the
       count had reached under the pill. */
    function () { if (pills().length > 2) pill(0).click(); },
    function () { if (pills().length > 2) out.back = snap(); }
  ];
  function tick() {
    if (!steps.length) { done(); return; }
    try { steps.shift()(); } catch (e) { document.title = "RESULTERROR:" + String(e); return; }
    setTimeout(tick, 30);
  }
  setTimeout(tick, 60);
})();
</script>
"""


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG 2.1 contrast ratio. AA body text is 4.5:1."""
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


@pytest.fixture(scope="module")
def multi():
    """760px: the width a claude.ai message column is on a laptop."""
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MULTI)),
        width=760,
    )


#: A LIGHT host: the page around the frame is white and the OS prefers
#: light. The card must look identical -- that is what "commits to one
#: look" means, and it is the failure mode a `prefers-color-scheme` branch
#: would reintroduce without anyone noticing.
LIGHT_HOST = """
<style>html, body { background: #ffffff !important; }</style>
"""


@pytest.fixture(scope="module")
def multi_light():
    return _run_page(
        _chrome_or_skip(),
        LIGHT_HOST + LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MULTI)),
        width=760,
        light=True,
    )


@pytest.fixture(scope="module")
def single():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(SINGLE)),
        width=760,
    )


class TestTheTopFiveAndTheButton:
    def test_exactly_five_rows_are_drawn_and_the_rest_are_not_in_the_dom(
        self, multi
    ):
        """Not five VISIBLE of thirty-six present: five rows exist. A row
        the reader cannot reach is a row that should not have been built."""
        assert multi["s0"]["rows"] == 5
        assert multi["s0"]["hscroll"] == 0, "a fare table must not scroll sideways"

    def test_nothing_scrolls_inside_the_card(self, multi):
        """The whole point of v3. An inner scrollbar in a chat message is
        the thing Matan was looking at when he said "wtf is that long
        scroll"."""
        assert multi["s0"]["vscroll"] == 0
        assert multi["s1"]["vscroll"] == 0
        assert multi["s2"]["vscroll"] == 0

    def test_the_button_names_the_step_and_what_is_left(self, multi):
        assert multi["s0"]["moreText"] == "Showing 5 of 36"
        assert multi["s0"]["moreBtn"] == "Show 5 more · 31 left"
        assert multi["s0"]["fewBtn"] is None, "nothing to collapse on page one"

    def test_one_tap_appends_five_and_a_second_tap_another_five(self, multi):
        assert multi["s1"]["rows"] == 10
        assert multi["s1"]["moreText"] == "Showing 10 of 36"
        assert multi["s1"]["moreBtn"] == "Show 5 more · 26 left"
        assert multi["s1"]["fewBtn"] == "Show fewer"
        assert multi["s2"]["rows"] == 15
        assert multi["s2"]["moreBtn"] == "Show 5 more · 21 left"

    def test_the_card_grows_with_what_it_draws(self, multi):
        """No ceiling, so a longer list is a taller card -- that is the
        host's scroll to do, not ours."""
        assert multi["s1"]["cardHeight"] > multi["s0"]["cardHeight"]
        assert multi["s2"]["cardHeight"] > multi["s1"]["cardHeight"]

    def test_show_fewer_goes_back_to_five(self, multi):
        assert multi["s3"]["rows"] == 5
        assert multi["s3"]["moreBtn"] == "Show 5 more · 31 left"
        assert multi["s3"]["fewBtn"] is None
        assert multi["s3"]["cardHeight"] == multi["s0"]["cardHeight"]

    def test_a_short_answer_gets_no_line_at_all(self, single):
        assert single["s0"]["rows"] == 3
        assert single["s0"]["vscroll"] == 0
        assert single["s0"]["more"] is None, "nothing to say about 3 of 3"


class TestTheFlightPowersLook:
    """Matan, 2026-09-22: "widget UI is WAYYYY too similar to google
    flights. do dark mode something cooler with flightpowers logo."

    Read off COMPUTED styles in a real engine, because "the stylesheet says
    ink-900" is a different claim from "the card is painted ink-900 here".
    """

    def test_the_card_is_painted_ink_900_not_white(self, multi):
        assert multi["cardBg"] == "rgb(12, 14, 17)", multi["cardBg"]
        assert multi["colorScheme"] == "dark"

    def test_the_text_clears_aa_on_that_ground(self, multi):
        """ink-200 #c9d1da on ink-900 #0c0e11 is 12.2:1; the muted ink-400
        #7d8794 used for second lines is 5.4:1. AA body text needs 4.5."""
        assert multi["bodyColor"] == "rgb(201, 209, 218)"
        assert _contrast((0xC9, 0xD1, 0xDA), (0x0C, 0x0E, 0x11)) > 4.5
        assert _contrast((0x7D, 0x87, 0x94), (0x0C, 0x0E, 0x11)) > 4.5
        assert _contrast((0xFF, 0xB0, 0x20), (0x0C, 0x0E, 0x11)) > 4.5
        # ...and the amber buttons invert: ink-900 text on signal-500.
        assert _contrast((0x0C, 0x0E, 0x11), (0xFF, 0xB0, 0x20)) > 4.5

    def test_the_robot_mark_and_the_wordmark_are_in_the_header(self, multi):
        assert multi["wordmark"] == "FlightPowers"
        assert multi["logoSize"] == "28x28"
        assert multi["logoBg"].startswith('url("data:image/png;base64,')

    def test_amber_is_the_only_accent_and_it_marks_the_cheapest(self, multi):
        """One accent, spent where the answer is: the cheapest fare, the
        selected pill, the gauge marker and the Show-more button. Google's
        blue is nowhere."""
        amber = "rgb(255, 176, 32)"
        assert multi["bestPriceColor"] == amber
        assert multi["selectedPillBg"] == amber
        assert multi["gaugeMarker"] == amber
        assert multi["moreBtnColor"] == amber
        assert multi["tags"] == "cheapest"
        # An ordinary fare is NOT amber -- if every row is lit, none is.
        assert multi["plainPriceColor"] == "rgb(232, 237, 242)"

    def test_prices_are_big_monospace_and_tabular(self, multi):
        assert multi["bestPriceSize"] == "16px"
        assert "ui-monospace" in multi["bestPriceFont"]

    def test_the_band_is_a_slim_glowing_gauge(self, multi):
        assert multi["gaugeHeight"] == 5, "a 5px track, not an 8px bar"
        assert multi["gaugeGlow"] >= 0, "the marker glows amber"

    def test_show_more_spans_the_card(self, multi):
        """A full-width ghost button: the one control under the list."""
        assert multi["moreBtnWidth"] >= 80, multi["moreBtnWidth"]

    def test_the_look_does_not_change_on_a_light_host(self, multi_light):
        """The frame is embedded in a WHITE page emulating a light host.
        Every one of these numbers is the dark card's."""
        assert multi_light["cardBg"] == "rgb(12, 14, 17)"
        assert multi_light["bodyColor"] == "rgb(201, 209, 218)"
        assert multi_light["bestPriceColor"] == "rgb(255, 176, 32)"
        assert multi_light["selectedPillBg"] == "rgb(255, 176, 32)"
        assert multi_light["s0"]["rows"] == 5


class TestTheDestinationPills:
    def test_one_pill_per_destination_plus_all_selected_by_default(self, multi):
        assert [p["text"] for p in multi["pills"]] == [
            "All36",
            "FCO12",
            "ATH12",
            "BUD12",
        ]
        assert [p["pressed"] for p in multi["pills"]] == [
            "true",
            "false",
            "false",
            "false",
        ]

    def test_a_single_destination_gets_no_pills(self, single):
        assert single["pills"] == []

    def test_picking_one_filters_the_rows_in_the_frame(self, multi):
        assert multi["f_pressed"] == ["false", "false", "true", "false"]
        assert multi["fs"]["rows"] == 5, "one page of Athens' twelve"
        # Athens' fares are the $2xx decade, and nothing else is.
        assert all(p.startswith("$2") for p in multi["f_prices"]), multi["f_prices"]
        assert multi["fs"]["moreText"] == "Showing 5 of 12"
        assert multi["fs"]["moreBtn"] == "Show 5 more · 7 left"

    def test_a_pill_resets_the_count_to_one_page(self, multi):
        """The tap history before it was 15 rows deep. A pill is a new
        question; landing on row 15 of a route just picked is not an answer
        to it."""
        assert multi["s2"]["rows"] == 15
        assert multi["fs"]["rows"] == 5
        assert multi["fs"]["fewBtn"] is None

    def test_the_band_and_the_cheapest_marker_are_recomputed(self, multi):
        """Google's band is per route. Rome's band left drawn over Athens'
        fares would be an invented number on the one element of the card
        that claims to be Google's own tracking."""
        assert "$118" in multi["band"] and "$240" in multi["band"]
        assert multi["badged"] == "$100cheapest"
        assert "$205" in multi["f_band"] and "$330" in multi["f_band"]
        assert "$118" not in multi["f_band"]
        assert multi["f_badged"] == "$200cheapest"

    def test_all_puts_every_destination_back_at_one_page(self, multi):
        assert multi["fs1"]["rows"] == 10, "one more page of Athens' twelve"
        assert multi["back"]["rows"] == 5
        assert multi["back"]["moreText"] == "Showing 5 of 36"


class TestWhichRouteARowIs:
    def test_under_all_every_row_names_its_destination(self, multi):
        """Sorted by price, the three routes interleave: without this
        column the All view is rows you cannot tell apart."""
        assert multi["headers"] == [
            "Depart",
            "To",
            "Airline",
            "Stops",
            "Duration",
            "Price",
            "Book",
        ]
        assert len(multi["dests"]) == 5
        assert set(multi["dests"]) <= {"FCO", "ATH", "BUD"}

    def test_the_column_survives_a_tap_on_show_more(self, multi):
        """The column is decided by how many routes the SELECTION holds,
        not by what is on screen -- otherwise page two of a three-route
        answer could lose it because its five rows happen to share a
        route."""
        assert len(multi["s1_dests"]) == 10

    def test_a_pill_takes_the_column_away_again(self, multi):
        assert "To" not in multi["f_headers"]
        assert multi["f_dests"] == []

    def test_a_single_destination_answer_never_has_the_column(self, single):
        assert single["headers"] == [
            "Depart",
            "Airline",
            "Stops",
            "Duration",
            "Price",
            "Book",
        ]
        assert single["dests"] == []

    def test_a_round_trip_names_it_after_the_two_legs(self, roundtrip):
        """Here the five cheapest happen to be one route (Rome is the $4xx
        decade, Athens the $7xx). The column is still drawn, because it is
        decided by how many routes the SELECTION holds -- a column that
        came and went as the reader tapped Show more would be worse than
        one that is sometimes uniform."""
        assert roundtrip["headers"][:4] == ["Outbound", "Return", "To", "Airline"]
        assert set(roundtrip["dests"]) == {"FCO"}
        assert len(roundtrip["dests"]) == 5
        assert "To" not in roundtrip["f_headers"]


class TestTheBandUnderSeveralRoutes:
    def test_it_says_it_is_one_route_of_several_and_which(self, multi):
        """Google tracks each route separately, so a band drawn over
        three of them at once is a number nobody published. The one drawn
        is the cheapest route's own -- $100 is Rome -- and the label says
        so instead of "for this route"."""
        assert multi["bandLabel"] == "Google price tracking · cheapest of 3 routes (FCO)"
        assert multi["badged"] == "$100cheapest"
        assert "$118" in multi["band"] and "$240" in multi["band"]
        assert "the route with the cheapest fare of these 3" in multi["band"]

    def test_one_destination_goes_back_to_this_route(self, multi):
        assert multi["f_bandLabel"] == "Google price tracking for this route"
        assert "$205" in multi["f_band"] and "$330" in multi["f_band"]

    def test_a_single_destination_answer_reads_as_it_always_did(self, single):
        assert single["bandLabel"] == "Google price tracking for this route"

    def test_the_band_follows_the_cheapest_route_on_a_round_trip(self, roundtrip):
        assert roundtrip["bandLabel"] == (
            "Google price tracking · cheapest of 2 routes (FCO)"
        )
        assert "$430" in roundtrip["band"] and "$610" in roundtrip["band"]


#: A round trip to two destinations: the rows are paired legs, priced as
#: a total, and they carry the same `to_airport` the one-way rows do -- so
#: the pills have to work there too, which is where a filter written
#: against one-way field names would quietly render an empty table.
ROUNDTRIP_MULTI = {
    "search_status": "ok",
    "result_count": 24,
    "search_coverage": {
        "destinations_searched": ["Athens (ATH)", "Rome (FCO)"],
        "departure_dates_searched": ["2026-11-10"],
    },
    "api_usage": {"requests_used_by_this_call": 2, "plan_requests_remaining": 46148},
    "results": sorted(
        (
            {
                "total_price": f"${base + i * 9}",
                "total_price_as_number": base + i * 9,
                "total_stops": i % 2,
                "from_airport": "Tel Aviv (TLV)",
                "to_airport": dest,
                "departure_date": "2026-11-10",
                "return_date": "2026-11-17",
                "departure_flight_departure_description": f"{_TIMES[i]} on Tue, Nov 10",
                "departure_flight_airline": airline,
                "departure_flight_duration": "3 hr 35 min",
                "return_flight_departure_description": f"{_TIMES[(i + 3) % 12]} on Tue, Nov 17",
                "return_flight_airline": airline,
                "return_flight_duration": "3 hr 25 min",
                "price_insights_low": low,
                "price_insights_high": high,
                "price_range_in_relation_to_other_periods": "typical",
                "buy_link": f"https://www.google.com/travel/flights?tfs=rt{dest[:3]}{i}",
            }
            for dest, base, airline, low, high in (
                ("Rome (FCO)", 400, "ITA Airways", 430, 610),
                ("Athens (ATH)", 700, "Aegean", 720, 980),
            )
            for i in range(12)
        ),
        key=lambda r: r["total_price_as_number"],
    ),
}


@pytest.fixture(scope="module")
def roundtrip():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(ROUNDTRIP_MULTI)),
        width=760,
    )


class TestAPairedLegRoundTrip:
    def test_the_pills_filter_paired_legs_too(self, roundtrip):
        assert [p["text"] for p in roundtrip["pills"]] == ["All24", "FCO12", "ATH12"]
        assert roundtrip["s0"]["rows"] == 5
        assert roundtrip["s0"]["moreText"] == "Showing 5 of 24"
        assert roundtrip["fs"]["rows"] == 5
        assert roundtrip["fs"]["moreText"] == "Showing 5 of 12"
        # Athens' round trips are the $7xx decade.
        assert all(p.startswith("$7") for p in roundtrip["f_prices"]), roundtrip[
            "f_prices"
        ]

    def test_the_band_follows_the_selected_destination(self, roundtrip):
        assert "$430" in roundtrip["band"] and "$610" in roundtrip["band"]
        assert "$720" in roundtrip["f_band"] and "$980" in roundtrip["f_band"]
        assert roundtrip["f_badged"] == "$700cheapest"


#: The LIVE shape, not a tidy one: the strings a real TLV -> Rome/Athens/
#: Budapest month actually returns. The fixtures above use "Aegean" and
#: three-letter codes; the answer returns "Israir Airlines" over "back:
#: Wizz Air" and "11:30 PM on Mon, Oct 26", and it was those that pushed
#: the dark card 16px sideways at 760 while every fixture here said 0.
LIVE_SHAPE = {
    "search_status": "ok",
    "result_count": 30,
    "search_coverage": {
        "destinations_searched": ["ATH", "BUD", "FCO"],
        "departure_dates_searched": [f"2026-10-{d:02d}" for d in range(1, 32)],
    },
    "api_usage": {
        "requests_used_by_this_call": 279,
        "hub_requests_billed": 279,
        "plan_requests_remaining": 44192,
    },
    "results": sorted(
        (
            {
                "total_price": f"${base + i * 3}",
                "total_price_as_number": base + i * 3,
                "total_stops": 0,
                "from_airport": "Tel Aviv (TLV)",
                "to_airport": dest,
                "departure_date": "2026-10-26",
                "return_date": "2026-10-30",
                "departure_flight_departure_description": "11:30 PM on Mon, Oct 26",
                "departure_flight_airline": out,
                "departure_flight_duration": "2 hr 15 min",
                "return_flight_departure_description": "12:55 AM on Thu, Oct 29",
                "return_flight_airline": back,
                "return_flight_duration": "2 hr 5 min",
                "price_insights_low": 115,
                "price_insights_high": 175,
                "price_range_in_relation_to_other_periods": "typical",
                "buy_link": f"https://www.google.com/travel/flights?tfs={dest}{i}",
            }
            for dest, base, out, back in (
                ("Athens (ATH)", 119, "Israir Airlines", "Wizz Air"),
                ("Rome (FCO)", 129, "ITA Airways", "Wizz Air"),
                ("Budapest (BUD)", 138, "Wizz Air", "Wizz Air"),
            )
            for i in range(10)
        ),
        key=lambda r: r["total_price_as_number"],
    ),
}


@pytest.fixture(scope="module")
def live_shape_760():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(LIVE_SHAPE)),
        width=760,
    )


@pytest.fixture(scope="module")
def live_shape_560():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(LIVE_SHAPE)),
        width=560,
    )


class TestTheRealAnswersOwnStrings:
    """The regression the tidy fixtures missed.

    Every other fixture here uses short airline names and bare IATA codes.
    The dark card's 16px prices and the CHEAPEST tag beside them pushed a
    seven-column row 16px past 760 on the strings the API actually returns,
    and nothing in this file noticed. Now something does.
    """

    def test_it_never_scrolls_sideways_at_a_message_width(self, live_shape_760):
        assert live_shape_760["s0"]["hscroll"] == 0
        assert live_shape_760["s0"]["vscroll"] == 0
        assert live_shape_760["s0"]["rows"] == 5

    def test_it_never_scrolls_sideways_on_a_narrow_frame_either(
        self, live_shape_560
    ):
        assert live_shape_560["s0"]["hscroll"] == 0
        assert live_shape_560["s0"]["rows"] == 5

    def test_the_cheapest_tag_sits_under_the_price_not_beside_it(
        self, live_shape_760
    ):
        """Inline, the tag made the price column 60px wider than its widest
        fare -- which was the 60px that decided whether the times wrapped."""
        assert live_shape_760["tagBelow"] is True
        assert live_shape_760["badged"] == "$119cheapest"


@pytest.fixture(scope="module")
def multi_640():
    """640px: the width at which the SEVENTH column stops fitting.

    The unwrapped-times breakpoint was 700 rather than 640 because of
    exactly this: with a destination column the row needs ~17px more than
    640 gives it, and the frame must wrap the times rather than put a
    sideways scrollbar under a fare table.
    """
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MULTI)),
        width=640,
    )


class TestTheNarrowerFrame:
    def test_seven_columns_never_scroll_sideways(self, multi_640):
        assert multi_640["s0"]["hscroll"] == 0
        assert multi_640["headers"][1] == "To"

    def test_it_still_shows_five_rows_just_taller_ones(self, multi_640):
        """v2's row count moved with the WIDTH, because a 780px ceiling
        decided it and a wrapped row is 86px. There is no ceiling any more:
        five rows are five rows at every width, and the card is whatever
        height that needs."""
        assert multi_640["s0"]["rows"] == 5
        assert multi_640["s0"]["moreText"] == "Showing 5 of 36"
        assert multi_640["s0"]["vscroll"] == 0


# ── the five findings of the zero-context review of #504 ────────────────
#
# Each one was measured in a browser, so each one is pinned in a browser.

#: A destination that arrives 400 characters long. Upstream data in a
#: nowrap cell: it measured a 3,370px sideways scroll before the cap.
LONG_DEST = "Rome Fiumicino Leonardo da Vinci International Airport " * 8

#: Two named routes and a third group of rows the upstream did not label,
#: with the CHEAPEST fare among the unlabelled ones -- the reviewer's
#: case: a $90 no-destination row marked against another route's band.
PARTIAL = {
    "search_status": "ok",
    "result_count": 9,
    "search_coverage": {"departure_dates_searched": ["2026-11-10"]},
    "api_usage": {"requests_used_by_this_call": 2, "plan_requests_remaining": 46150},
    "results": (
        [
            {
                "price": "$90",
                "price_as_number": 90,
                "airline": "Unlabelled Air",
                "stops": 0,
                "duration": "3 hr",
                "departure_description": "6:05 AM on Tue, Nov 10",
                "from_airport": "Tel Aviv (TLV)",
                "buy_link": "https://www.google.com/travel/flights?tfs=none",
            }
        ]
        + [
            {
                "price": f"${250 + i * 10}",
                "price_as_number": 250 + i * 10,
                "airline": "ITA Airways",
                "stops": 0,
                "duration": "4 hr",
                "departure_description": f"{_TIMES[i]} on Tue, Nov 10",
                "from_airport": "Tel Aviv (TLV)",
                "to_airport": LONG_DEST if i == 0 else "Athens (ATH)",
                "price_insights_low": 250,
                "price_insights_high": 420,
                "price_range_in_relation_to_other_periods": "typical",
                "buy_link": f"https://www.google.com/travel/flights?tfs=p{i}",
            }
            for i in range(8)
        ]
    ),
}

#: Exactly ten fares, one destination: the count the old early return
#: never capped. Ten 184px block rows are a 2,029px card.
TEN = {
    "search_status": "ok",
    "result_count": 10,
    "search_coverage": {
        "destinations_searched": ["Budapest (BUD)"],
        "departure_dates_searched": ["2026-11-10"],
    },
    "api_usage": {"requests_used_by_this_call": 1, "plan_requests_remaining": 46152},
    "results": [r for r in _rows() if r["to_airport"] == "Budapest (BUD)"][:10],
}


@pytest.fixture(scope="module")
def multi_narrow():
    """A phone-width frame: block rows, and the band has to give way.

    Asked for 380; Chrome will not open a window narrower than 500, which
    is still inside the under-520 layout this is here to measure.
    """
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MULTI)),
        width=380,
    )


@pytest.fixture(scope="module")
def partial():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(PARTIAL)),
        width=760,
    )


@pytest.fixture(scope="module")
def ten_narrow():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(TEN)),
        width=380,
    )


class TestANarrowFrame:
    def test_the_band_collapses_to_one_line(self, multi_narrow):
        assert multi_narrow["bandBar"] is False, "the 70px bar is gone below 520px"
        assert multi_narrow["bandLine"] == (
            "Google: typical $118–$240 · cheapest of 3 routes (FCO)"
        )

    def test_five_fares_are_on_screen_here_too(self, multi_narrow):
        """v2 measured TWO of thirty-six at this width, then five once a
        floor was added, because the header, pills, band and footer ate
        most of a 780px budget. Without a budget the question does not
        arise: five rows, no floor logic, no sideways scroll."""
        assert multi_narrow["s0"]["rows"] == 5
        assert multi_narrow["s0"]["moreText"] == "Showing 5 of 36"
        assert multi_narrow["s0"]["hscroll"] == 0
        assert multi_narrow["s0"]["vscroll"] == 0

    def test_a_ten_row_answer_pages_in_fives(self, ten_narrow):
        """Two pages exactly. The button drops its "N left" tail on the
        last one, where "Show 5 more · 5 left" says the same thing twice,
        and once everything is shown only "Show fewer" is left."""
        assert ten_narrow["s0"]["rows"] == 5
        assert ten_narrow["s0"]["moreBtn"] == "Show 5 more"
        assert ten_narrow["s1"]["rows"] == 10
        assert ten_narrow["s1"]["moreBtn"] is None
        assert ten_narrow["s1"]["more"] == "Showing 10 of 10Show fewer"
        assert ten_narrow["s0"]["vscroll"] == 0


class TestRowsTheUpstreamDidNotLabel:
    def test_they_get_their_own_pill_instead_of_vanishing(self, partial):
        assert [p["text"] for p in partial["pills"]] == [
            "All9",
            "Rome Fiumicino…1",
            "ATH7",
            "Other1",
        ]

    def test_no_band_is_drawn_rather_than_another_routes(self, partial):
        """$90 has no route of its own, and every band on offer belongs to
        one of the other two."""
        assert partial["badged"] == "$90cheapest"
        assert partial["bandBar"] is False
        assert partial["bandLine"] is None
        assert partial["bandLabel"] is None

    def test_a_pill_that_does_have_a_band_still_draws_it(self, partial):
        """Pill 2 is ATH: one route, its own numbers, and the label goes
        back to "for this route"."""
        assert partial["fs"]["rows"] == 5
        assert partial["f_bandLabel"] == "Google price tracking for this route"
        assert "$250" in partial["f_band"] and "$420" in partial["f_band"]


class TestALongDestinationString:
    def test_the_label_is_capped_and_the_full_value_kept(self, partial):
        assert "Rome Fiumicino…" in [p["text"].rstrip("0123456789") for p in partial["pills"]]
        labels = [d for d in partial["dests"] if d.startswith("Rome")]
        assert labels == ["Rome Fiumicino…"]
        assert len(labels[0]) == 15
        # `txt()` trims, so the title is the trimmed upstream string.
        assert LONG_DEST.strip() in partial["destTitles"], (
            "the full value must survive as a title"
        )

    def test_it_cannot_widen_the_table(self, partial):
        assert partial["s0"]["hscroll"] == 0, (
            "a 400-character destination scrolled 3,370px"
        )


# ── a whole month across three destinations, and a refusal ──────────────
#
# The cap that made one call cover a month changed what reaches this frame:
# 93 rows for one destination, 186 for two, and -- since the 300 cap of
# 2026-09-22 -- 279 combinations for a month x 3 nights x 3 destinations,
# where the card was designed against 36. That volume is exactly what
# produced "wtf is that long scroll", so the assertion here is that the DOM
# holds ten rows however many fares arrived, that nothing scrolls inside the
# card, and that the work done per paint does not grow with the answer.
# Plus one thing that must not regress: a search REFUSED for want of quota
# arrives with no rows at all and must draw a small card, never an empty
# table (src/quota_gate.py).

MONTH_ROWS = {
    "search_status": "ok",
    "result_count": 279,
    "search_coverage": {
        "requested_combinations": 279,
        "searched_combinations": 279,
        "truncated": False,
        "max_searches_source": "auto_span",
        "destinations_searched": ["ATH", "BUD", "FCO"],
    },
    "api_usage": {
        "requests_used_by_this_call": 279,
        "hub_requests_billed": 279,
        "plan_requests_remaining": 2314,
    },
    "results": sorted(
        (
            {
                "price": f"${base + i}",
                "price_as_number": base + i,
                "total_price_as_number": base + i,
                "airline": airline,
                "stops": i % 2,
                "duration": "4 hr 55 min",
                "departure_description": "08:15 AM on Thu, Oct 1",
                "arrival_description": "arrives 11:10 AM",
                "from_airport": "TLV",
                "to_airport": dest,
                "price_insights_low": low,
                "price_insights_high": high,
                "price_range_in_relation_to_other_periods": "typical",
                "buy_link": f"https://www.google.com/travel/flights?tfs={dest}{i}",
            }
            for dest, base, airline, low, high in (
                ("FCO", 300, "ITA Airways", 320, 540),
                ("ATH", 600, "Aegean", 610, 870),
                ("BUD", 900, "Wizz Air", 910, 1180),
            )
            for i in range(93)
        ),
        key=lambda r: r["total_price_as_number"],
    ),
}

REFUSAL = {
    "search_status": "quota_exceeded",
    "results": [],
    "result_count": 0,
    "retry": False,
    "combos_requested": 93,
    "combos_allowed_now": 9,
    "remaining_month": 9,
    "requests_spent": 1,
    "message": (
        "This search needs 93 requests but your plan has 9 left this month "
        "(plan quota 10 a month). Ask for fewer dates or nights (e.g. one "
        "week, 3 nights), or move to PRO ($10/month, 2,500 requests) at "
        "https://rapidapi.com/mtnrabi/api/google-flights-live-api."
    ),
}

REFUSAL_PROBE = r"""
<script>
(function () {
  var out = {};
  window.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                       params: { structuredContent: __PAYLOAD__ } }, "*");
  setTimeout(function () {
    out.tables = document.querySelectorAll("table.fp-t").length;
    out.pills = document.querySelectorAll("button.fp-pill").length;
    var q = document.querySelector(".fp-quota");
    out.quota = q ? q.textContent.replace(/\s+/g, " ").trim() : null;
    var n = document.querySelector(".fp-note");
    out.note = n ? n.textContent.replace(/\s+/g, " ").trim() : null;
    out.cardHeight = Math.round(document.querySelector(".fp-card").getBoundingClientRect().height);
    out.hscroll = document.documentElement.scrollWidth - document.documentElement.clientWidth;
    try { document.title = "RESULT:" + btoa(unescape(encodeURIComponent(JSON.stringify(out)))); }
    catch (e) { document.title = "RESULTERROR:" + String(e); }
  }, 80);
})();
</script>
"""


@pytest.fixture(scope="module")
def month():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MONTH_ROWS)),
        width=760,
    )


@pytest.fixture(scope="module")
def refusal():
    return _run_page(
        _chrome_or_skip(),
        REFUSAL_PROBE.replace("__PAYLOAD__", _inline(REFUSAL)),
        width=760,
    )


class TestAWholeMonthAcrossThreeDestinations:
    """279 combinations: a month x 3 nights x 3 destinations, the exact
    question the 300 cap was raised for and the volume behind "wtf is that
    long scroll"."""

    def test_two_hundred_and_seventy_nine_fares_draw_five_rows(self, month):
        assert month["s0"]["rows"] == 5
        assert month["s0"]["moreText"] == "Showing 5 of 279"
        assert month["s0"]["moreBtn"] == "Show 5 more · 274 left"

    def test_nothing_scrolls_inside_the_card_at_this_volume(self, month):
        assert month["s0"]["vscroll"] == 0
        assert month["s0"]["hscroll"] == 0
        assert month["s2"]["vscroll"] == 0

    def test_taps_walk_the_list_five_at_a_time(self, month):
        assert month["s1"]["rows"] == 10
        assert month["s2"]["rows"] == 15
        assert month["s2"]["moreBtn"] == "Show 5 more · 264 left"
        assert month["s3"]["rows"] == 5

    def test_the_pills_group_the_three_destinations(self, month):
        assert [p["text"] for p in month["pills"]] == [
            "All279",
            "FCO93",
            "ATH93",
            "BUD93",
        ]
        assert month["fs"]["rows"] == 5
        assert month["fs"]["moreText"] == "Showing 5 of 93"

    def test_all_puts_the_whole_month_back_at_one_page(self, month):
        assert month["fs1"]["rows"] == 10
        assert month["back"]["rows"] == 5
        assert month["back"]["moreText"] == "Showing 5 of 279"

    def test_the_cheapest_of_the_month_is_the_one_badged(self, month):
        assert month["badged"] == "$300cheapest"


class TestARefusedSearch:
    """No rows, so no table: the two numbers and the message, nothing else."""

    def test_it_is_a_small_card_with_no_table_and_no_pills(self, refusal):
        assert refusal["tables"] == 0
        assert refusal["pills"] == 0
        assert refusal["cardHeight"] <= 780
        assert refusal["hscroll"] == 0

    def test_the_two_numbers_are_the_headline(self, refusal):
        assert refusal["quota"] == "93 requests needed · 9 left"

    def test_both_ways_forward_are_on_the_card(self, refusal):
        assert "fewer dates or nights" in refusal["note"]
        assert "PRO ($10/month, 2,500 requests)" in refusal["note"]


# A refusal now carries the ONE combination's fares (they were billed), so
# the card has both a refusal headline and a table. Pinned here because the
# two halves have to agree: the numbers say 93 were asked for, the table
# shows one date's worth, and the message is what reconciles them.
REFUSAL_WITH_FARES = dict(
    REFUSAL,
    result_count=2,
    combos_searched=1,
    results=[
        {
            "price": "$412",
            "price_as_number": 412,
            "airline": "ITA Airways",
            "stops": 0,
            "duration": "4 hr 5 min",
            "departure_description": "08:15 AM on Thu, Oct 1",
            "from_airport": "TLV",
            "to_airport": "FCO",
            "price_insights_low": 320,
            "price_insights_high": 540,
            "price_range_in_relation_to_other_periods": "typical",
            "buy_link": "https://www.google.com/travel/flights?tfs=a",
        },
        {
            "price": "$488",
            "price_as_number": 488,
            "airline": "Wizz Air",
            "stops": 1,
            "duration": "7 hr",
            "departure_description": "06:00 PM on Thu, Oct 1",
            "from_airport": "TLV",
            "to_airport": "FCO",
            "buy_link": "https://www.google.com/travel/flights?tfs=b",
        },
    ],
)

REFUSAL_ROWS_PROBE = REFUSAL_PROBE.replace(
    'out.cardHeight =',
    'out.rows = document.querySelectorAll("table.fp-t tbody tr").length;\n'
    "    out.cardHeight =",
)


@pytest.fixture(scope="module")
def refusal_with_fares():
    return _run_page(
        _chrome_or_skip(),
        REFUSAL_ROWS_PROBE.replace("__PAYLOAD__", _inline(REFUSAL_WITH_FARES)),
        width=760,
    )


class TestARefusalThatStillHasFares:
    def test_the_paid_for_fares_are_drawn(self, refusal_with_fares):
        assert refusal_with_fares["rows"] == 2
        assert refusal_with_fares["tables"] == 1

    def test_the_refusal_headline_is_still_above_them(self, refusal_with_fares):
        assert refusal_with_fares["quota"] == "93 requests needed · 9 left"
        assert "9 left this month" in refusal_with_fares["note"]

    def test_one_destination_means_no_pills(self, refusal_with_fares):
        assert refusal_with_fares["pills"] == 0
        assert refusal_with_fares["cardHeight"] <= 780
        assert refusal_with_fares["hscroll"] == 0
