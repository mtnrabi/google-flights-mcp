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


def _run_page(chrome: str, probe: str, width: int = 800) -> dict:
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


# ── the top ten, and the destination pills ──────────────────────────────
#
# Matan, 2026-09-22, looking at the live card: "widget should not be that
# long: showcase the top 10, scrollable for more" and "the widget should
# have a button for selecting each destination if multiple were selected,
# default is all of them".
#
# Both are layout behaviour, which is exactly the kind of claim a bytes
# assertion cannot make: "the code sets maxHeight" is not "ten rows are
# visible and the eleventh is not". So they are measured here, in a real
# engine, at a stated width.

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
  /* Visible means visible: inside the scroll box's own painted area. A
     row below its bottom edge is one the reader has to scroll to. */
  function visible() {
    var b = box().getBoundingClientRect(), rs = rows(), n = 0;
    for (var i = 0; i < rs.length; i++) {
      if (rs[i].getBoundingClientRect().bottom > b.bottom + 0.5) break;
      n++;
    }
    return n;
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
  function more() {
    var m = document.querySelector(".fp-more");
    return m ? m.textContent.replace(/\s+/g, " ").trim() : null;
  }
  window.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                       params: { structuredContent: __PAYLOAD__ } }, "*");
  setTimeout(function () {
    out.width = window.innerWidth;
    out.pills = pills();
    out.rows = rows().length;
    out.visible = visible();
    out.capped = box().getAttribute("data-capped");
    out.scrollable = box().scrollHeight > box().clientHeight + 1;
    out.hscroll = box().scrollWidth - box().clientWidth;
    out.cardHeight = Math.round(document.querySelector(".fp-card").getBoundingClientRect().height);
    out.more = more();
    out.band = band();
    out.bandLabel = bandLabel();
    out.bandLine = bandLine();
    out.bandBar = !!document.querySelector(".fp-bar");
    out.destTitles = destTitles();
    out.headers = headers();
    out.dests = dests();
    out.badged = badged();
    out.stickyHead = getComputedStyle(document.querySelector("table.fp-t th")).position;
    if (out.pills.length > 2) {
      /* Pill 2 is the second destination -- pill 0 is All. */
      document.querySelectorAll("button.fp-pill")[2].click();
      setTimeout(function () {
        out.f_pressed = pills().map(function (p) { return p.pressed; });
        out.f_rows = rows().length;
        out.f_visible = visible();
        out.f_prices = prices();
        out.f_band = band();
        out.f_bandLabel = bandLabel();
        out.f_bandLine = bandLine();
        out.f_headers = headers();
        out.f_dests = dests();
        out.f_badged = badged();
        out.f_more = more();
        out.f_cardHeight = Math.round(document.querySelector(".fp-card").getBoundingClientRect().height);
        var btn = document.querySelector(".fp-morebtn");
        if (btn) btn.click();
        setTimeout(function () {
          out.e_visible = visible();
          out.e_capped = box().getAttribute("data-capped");
          out.e_more = more();
          /* ...and back to All, which must restore the whole answer. */
          document.querySelectorAll("button.fp-pill")[0].click();
          setTimeout(function () {
            out.back_rows = rows().length;
            out.back_visible = visible();
            done();
          }, 40);
        }, 40);
      }, 40);
    } else { done(); }
  }, 60);
})();
</script>
"""


@pytest.fixture(scope="module")
def multi():
    """760px: the width a claude.ai message column is on a laptop."""
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(MULTI)),
        width=760,
    )


@pytest.fixture(scope="module")
def single():
    return _run_page(
        _chrome_or_skip(),
        LAYOUT_PROBE.replace("__PAYLOAD__", _inline(SINGLE)),
        width=760,
    )


class TestTheTopTen:
    def test_ten_rows_are_visible_and_the_other_twenty_six_are_a_scroll_away(
        self, multi
    ):
        assert multi["rows"] == 36, "every fare is still in the table"
        assert multi["visible"] == 10
        assert multi["scrollable"] is True
        assert multi["hscroll"] == 0, "a fare table must not scroll sideways"

    def test_the_card_stays_inside_its_height_budget(self, multi):
        assert multi["cardHeight"] <= 780, (
            "the card is back to being a wall: " + str(multi["cardHeight"]) + "px"
        )

    def test_the_footer_says_how_many_of_how_many(self, multi):
        assert multi["more"] == "Showing 10 of 36 · scroll for moreShow all 36"

    def test_the_column_names_survive_the_scroll(self, multi):
        assert multi["stickyHead"] == "sticky"

    def test_show_all_uncaps_the_table(self, multi):
        """For a host that sizes the frame to its content and swallows the
        inner scroll, this button is the only way to rows 11 and up."""
        assert multi["e_capped"] is None
        assert multi["e_visible"] == 12, "the filtered destination's 12 fares"
        assert multi["e_more"] == "Showing all 12 faresShow top 10"

    def test_a_short_answer_is_not_capped_at_all(self, single):
        assert single["rows"] == 3
        assert single["visible"] == 3
        assert single["scrollable"] is False
        assert single["more"] is None, "nothing to say about 3 of 3"


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
        assert multi["f_rows"] == 12
        # Athens' fares are the $2xx decade, and nothing else is.
        assert all(p.startswith("$2") for p in multi["f_prices"]), multi["f_prices"]
        assert multi["f_more"] == "Showing 10 of 12 · scroll for moreShow all 12"

    def test_the_band_and_the_cheapest_marker_are_recomputed(self, multi):
        """Google's band is per route. Rome's band left drawn over Athens'
        fares would be an invented number on the one element of the card
        that claims to be Google's own tracking."""
        assert "$118" in multi["band"] and "$240" in multi["band"]
        assert multi["badged"] == "$100cheapest"
        assert "$205" in multi["f_band"] and "$330" in multi["f_band"]
        assert "$118" not in multi["f_band"]
        assert multi["f_badged"] == "$200cheapest"

    def test_all_puts_every_destination_back(self, multi):
        assert multi["back_rows"] == 36
        assert multi["back_visible"] == 10


class TestWhichRouteARowIs:
    def test_under_all_every_row_names_its_destination(self, multi):
        """Sorted by price, the three routes interleave: without this
        column the All view is 36 rows you cannot tell apart."""
        assert multi["headers"] == [
            "Depart",
            "To",
            "Airline",
            "Stops",
            "Duration",
            "Price",
            "Book",
        ]
        assert len(multi["dests"]) == 36
        assert set(multi["dests"]) == {"FCO", "ATH", "BUD"}

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
        assert roundtrip["headers"][:4] == ["Outbound", "Return", "To", "Airline"]
        assert set(roundtrip["dests"]) == {"FCO", "ATH"}
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
        assert roundtrip["rows"] == 24
        assert roundtrip["visible"] == 10
        assert roundtrip["f_rows"] == 12
        # Athens' round trips are the $7xx decade.
        assert all(p.startswith("$7") for p in roundtrip["f_prices"]), roundtrip[
            "f_prices"
        ]

    def test_the_band_follows_the_selected_destination(self, roundtrip):
        assert "$430" in roundtrip["band"] and "$610" in roundtrip["band"]
        assert "$720" in roundtrip["f_band"] and "$980" in roundtrip["f_band"]
        assert roundtrip["f_badged"] == "$700cheapest"


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
        assert multi_640["hscroll"] == 0
        assert multi_640["headers"][1] == "To"

    def test_it_shows_fewer_rows_and_says_the_real_number(self, multi_640):
        """Wrapped rows are 86px, so ten of them do not fit the ceiling.
        The footer prints what is on screen, not the ten it aimed for."""
        assert multi_640["visible"] == 5
        assert multi_640["more"] == "Showing 5 of 36 · scroll for moreShow all 36"
        assert multi_640["cardHeight"] <= 780


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

    def test_at_least_five_fares_are_on_screen(self, multi_narrow):
        """Two of thirty-six was the measurement that opened this. The
        floor wins over the ceiling; the footer still says the truth."""
        assert multi_narrow["visible"] >= 5
        assert multi_narrow["more"].startswith("Showing 5 of 36")
        assert multi_narrow["hscroll"] == 0

    def test_ten_rows_are_capped_too(self, ten_narrow):
        """The old early return left a ten-row answer uncapped: a 2,029px
        card with no scroll and no way to see it was capped at all."""
        assert ten_narrow["rows"] == 10
        assert ten_narrow["capped"] == "1"
        assert ten_narrow["scrollable"] is True
        assert ten_narrow["visible"] == 5
        assert ten_narrow["more"] == "Showing 5 of 10 · scroll for moreShow all 10"
        assert ten_narrow["cardHeight"] < 1400, ten_narrow["cardHeight"]


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
        assert partial["f_rows"] == 7
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
        assert partial["hscroll"] == 0, "a 400-character destination scrolled 3,370px"
