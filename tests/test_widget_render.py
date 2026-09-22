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


def _run(chrome: str) -> dict:
    page = FLIGHTS_WIDGET_HTML + PROBE.replace(
        "__HOSTILE__", _inline(HOSTILE)
    ).replace("__BENIGN__", _inline(BENIGN))
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


@pytest.fixture(scope="module")
def rendered():
    chrome = _chrome()
    if not chrome:
        pytest.skip(
            "no Chrome binary on this machine, so the widget's JS cannot be "
            "executed here; the same rules are pinned as bytes assertions in "
            "tests/test_widget.py"
        )
    return _run(chrome)


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
