"""
Public policy pages, served by the server they describe.

Every app-directory submission demands a privacy policy URL and a terms URL
that resolve for an anonymous visitor over HTTPS with no login. A Markdown file
in a repo does not satisfy a reviewer, and `flightpowers.com/privacy` is a
different deployment (mrabi's Next.js product) that this package has no
business redeploying for three static pages. So the server that is being
submitted hosts its own policies.

The renderer is deliberately a closed subset rather than a Markdown library.
The two source documents use exactly six constructs -- headings, tables,
unordered lists, bold, inline code, and horizontal rules -- with no links, no
code fences and no ordered lists, which `test_legal.py` asserts so that adding
an unsupported construct fails a test instead of silently rendering as literal
text on a page a reviewer is reading. Adding a dependency to render 15 KB of
static prose would be the worse trade.

Editing the policies means editing `legal/*.md`; there is no build step.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"

CONTACT_EMAIL = "mtnrabi@gmail.com"

_STYLE = """
:root { color-scheme: light dark; }
body {
  margin: 0 auto; padding: 3rem 1.25rem 6rem; max-width: 46rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 1.5rem; }
h2 { font-size: 1.2rem; margin: 2.5rem 0 .75rem; }
h3 { font-size: 1rem; margin: 1.75rem 0 .5rem; }
hr { border: 0; border-top: 1px solid rgba(128,128,128,.3); margin: 2.5rem 0; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .95rem; }
th, td { border: 1px solid rgba(128,128,128,.35); padding: .5rem .6rem;
         text-align: left; vertical-align: top; }
th { background: rgba(128,128,128,.1); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       font-size: .9em; background: rgba(128,128,128,.15);
       padding: .1em .35em; border-radius: 3px; }
ul { padding-left: 1.3rem; }
li { margin: .3rem 0; }
a { color: inherit; }
nav { margin-bottom: 2.5rem; font-size: .9rem; }
nav a { margin-right: 1rem; }
"""

_NAV = (
    '<nav><a href="/privacy">Privacy</a><a href="/terms">Terms</a>'
    '<a href="/support">Support</a>'
    # Relative, so the hotels deployment's nav reports the hotels
    # deployment's health rather than the flights one's.
    '<a href="/health">Status</a></nav>'
)


def _inline(text: str) -> str:
    """Escape, then re-introduce the two inline constructs we support.

    Escaping first is what makes this safe: the policy text is trusted, but a
    renderer that interpolates before escaping is a habit that eventually meets
    untrusted input.
    """
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def _render_table(rows: list[str]) -> str:
    """A GitHub-style pipe table. `rows` excludes nothing; row 1 is the header
    and row 2 is the `|---|` separator, which is dropped."""
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    out = ["<table><thead><tr>"]
    out.extend(f"<th>{_inline(c)}</th>" for c in head)
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def markdown_to_html(source: str) -> str:
    """Render the closed subset. Anything unrecognised becomes a paragraph,
    which keeps the page readable rather than dropping content silently."""
    lines = source.splitlines()
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []
    table: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            # extend, not `out +=`: augmented assignment would rebind `out` as
            # a local of this closure and raise UnboundLocalError.
            out.append("<ul>")
            out.extend(f"<li>{_inline(b)}</li>" for b in bullets)
            out.append("</ul>")
            bullets.clear()

    def flush_table() -> None:
        if table:
            out.append(_render_table(list(table)))
            table.clear()

    def flush_all() -> None:
        flush_para(); flush_bullets(); flush_table()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("|"):
            flush_para(); flush_bullets()
            table.append(stripped)
            continue
        flush_table()

        if not stripped:
            flush_para(); flush_bullets()
            continue

        if re.fullmatch(r"-{3,}", stripped):
            flush_all()
            out.append("<hr>")
            continue

        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            flush_all()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        bullet = re.match(r"[-*]\s+(.*)", stripped)
        if bullet:
            flush_para()
            bullets.append(bullet.group(1))
            continue

        flush_bullets()
        para.append(stripped)

    flush_all()
    return "\n".join(out)


#: The tab icon, inline rather than a file.
#:
#: Every browser that opens /connect, /privacy or the consent page asks for a
#: favicon, and every one of those asks was a 404 in the logs -- noise that
#: makes a real 404 harder to see, and a blank tab next to a page that is
#: asking a user to paste an API key. 300 bytes of SVG served from the same
#: process is the whole fix: no binary in the repo, no build step, and
#: nothing extra in the Vercel bundle.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#1f6feb"/>'
    '<path d="M7 17.6l17.5-8.2c.9-.4 1.8.5 1.4 1.4l-8.2 17.5c-.4.9-1.7.8-2-.2'
    'l-2-6.3-6.3-2c-1-.3-1.1-1.6-.2-2z" fill="#fff"/>'
    "</svg>"
)

_HEAD_ICON = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'


def page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{_HEAD_ICON}"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{_NAV}{body_html}</body></html>"
    )


# Per-product substitutions for the legal documents.
#
# One set of documents, three deployments. Serving the flights privacy policy
# from the hotels host is not a cosmetic slip: a policy that names the wrong
# service and the wrong upstream processor is simply not a policy for that
# service, and it is the first thing a store reviewer reads.
PRODUCT_CONTEXT: dict[str, dict[str, str]] = {
    "flights": {
        "PRODUCT": "Google Flights MCP",
        "HOST": "flights.flightpowers.com",
        "REGISTRY_NAME": "com.flightpowers/google-flights",
        "UPSTREAM_API": "Google Flights Live API",
        "DATA_NOUN": "flight",
        "TOOL_NAMES": "`search_oneway_flights` or `search_roundtrip_flights`",
        "TOOL_BULLETS": "- `search_oneway_flights`\n- `search_roundtrip_flights`",
        "NOT_AFFILIATED": "Google",
        "SIGNUP_URL": "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
        # What the tools actually do with a request. Was a fixed paragraph
        # describing the flight fan-out, which is not what the hotel tools do
        # and not a cap that applies to them.
        "TOOL_BEHAVIOUR": (
            "Both accept a date range and a list of destinations, expand them "
            "internally into individual searches (30 per call by default; a "
            "per-call `max_searches` argument can lower that figure for a "
            "single call but cannot raise it, and 60 is the hard ceiling the "
            "deployment-wide setting itself cannot exceed), and forward each "
            "search to the Google Flights Live API on RapidAPI."
        ),
        "SEARCH_PARAM_NOUNS": (
            "Origins, destinations, dates, passenger counts, cabin class, "
            "price limits and airline filters"
        ),
        "RESULT_NOUN": "Flight results",
        "RESULT_SENTENCE": (
            "Fares are returned to you and not retained. Fares go stale "
            "within minutes, so nothing is cached or reused."
        ),
        "COST_SENTENCE": (
            "Cost of a fan-out is roughly one billed upstream request per "
            "date/destination combination searched"
        ),
        "BRAND_NOUNS": "Airline, airport and travel-brand names",
        "STALENESS_BULLET": (
            "**Fares change constantly and go stale within minutes.** Every "
            "result is a snapshot of the moment it was fetched. Do not cache "
            "fares, do not reuse an earlier result, and do not present a "
            "previously fetched fare as current. If you display a fare, "
            "display when it was fetched."
        ),
        "CONFIRMATION_SITES": "the airline or booking site",
        "EMPTY_RESULT_BULLET": (
            "**An empty result set is a valid answer**, meaning no flights "
            "were found for that route and those dates. It is not an error."
        ),
        "PRICE_CONTEXT_BULLET": (
            "**Price insight fields** (`price_range_in_relation_to_other_"
            "periods`, `price_insights_low`, `price_insights_high`) are the "
            "upstream provider's historical characterisation of a route and "
            "period. They are context, not a prediction or financial advice."
        ),
    },
    "hotels": {
        "PRODUCT": "Booking.com Hotels MCP",
        "HOST": "hotels.flightpowers.com",
        "REGISTRY_NAME": "com.flightpowers/booking",
        "UPSTREAM_API": "Booking Live API",
        "DATA_NOUN": "hotel",
        "TOOL_NAMES": "`search_hotels` or `find_hotel_by_name`",
        "TOOL_BULLETS": "- `search_hotels`\n- `find_hotel_by_name`",
        "NOT_AFFILIATED": "Booking.com",
        "SIGNUP_URL": "https://rapidapi.com/mtnrabi/api/booking-live-api",
        "TOOL_BEHAVIOUR": (
            "Both take a stay -- a destination or a property name, plus "
            "check-in and check-out dates -- and forward it to the Booking "
            "Live API on RapidAPI as a single request. There is no fan-out: "
            "one tool call is exactly one upstream request. An optional "
            "two-letter country code prices the stay through a residential "
            "connection in that country, so the result is what a shopper "
            "resident there would be quoted."
        ),
        "SEARCH_PARAM_NOUNS": (
            "Destinations, property names, stay dates, guest counts, currency, "
            "nightly budget, property filters and the country a price is "
            "requested from"
        ),
        "RESULT_NOUN": "Hotel results",
        "RESULT_SENTENCE": (
            "Room rates are returned to you and not retained. Rates go stale "
            "within minutes, so nothing is cached or reused."
        ),
        "COST_SENTENCE": (
            "There is no fan-out here: each tool call is exactly one billed "
            "upstream request"
        ),
        "BRAND_NOUNS": "Property, chain and travel-brand names",
        "STALENESS_BULLET": (
            "**Room rates change constantly and go stale within minutes.** "
            "Every result is a snapshot of the moment it was fetched. Do not "
            "cache a rate, do not reuse an earlier result, and do not present "
            "a previously fetched rate as current. If you display a rate, "
            "display when it was fetched, and the country it was priced from "
            "if one was given."
        ),
        "CONFIRMATION_SITES": "the property or booking site",
        "EMPTY_RESULT_BULLET": (
            "**An empty result set is a valid answer**, meaning no bookable "
            "property matched that destination, those dates and any filters "
            "applied. It is not an error."
        ),
        "PRICE_CONTEXT_BULLET": (
            "**A price requested from a given country** is what a shopper "
            "resident there was quoted at that moment, not a guarantee that "
            "the same rate is available to you. Rates differ by country, by "
            "currency and over time; treat a difference between two countries "
            "as an observation, not as advice."
        ),
    },
}
# The combined deployment sells both listings, so it inherits neither product's
# upstream, non-affiliation or tool list wholesale: a policy naming only the
# Google Flights Live API, and disclaiming affiliation only with Google, is not
# a policy for the hotel half of the same server.
PRODUCT_CONTEXT["both"] = dict(
    PRODUCT_CONTEXT["flights"],
    PRODUCT="Flight & Hotel Data MCP",
    HOST="google-flights-mcp.flightpowers.com",
    DATA_NOUN="flight and hotel",
    UPSTREAM_API="Google Flights Live API and the Booking Live API",
    NOT_AFFILIATED="Google or Booking.com",
    TOOL_NAMES=(
        "`search_oneway_flights`, `search_roundtrip_flights`, "
        "`search_hotels` or `find_hotel_by_name`"
    ),
    TOOL_BULLETS=(
        "- `search_oneway_flights`\n- `search_roundtrip_flights`\n"
        "- `search_hotels`\n- `find_hotel_by_name`"
    ),
    # Written out rather than concatenated: the two single-product paragraphs
    # each open with "Both", which is four tools between them, and one denies
    # the fan-out the other has just described.
    TOOL_BEHAVIOUR=(
        "The two flight tools accept a date range and a list of destinations "
        "and expand them internally into individual searches (30 per call by "
        "default; a per-call `max_searches` argument can lower that figure "
        "for a single call but cannot raise it, and 60 is the hard ceiling "
        "the deployment-wide setting itself cannot exceed), forwarding each "
        "search to the Google Flights Live API on RapidAPI. The two hotel "
        "tools take one stay -- a destination or a property name, plus "
        "check-in and check-out dates -- and forward it to the Booking Live "
        "API on RapidAPI as a single request, with no fan-out: one hotel tool "
        "call is exactly one upstream request. An optional two-letter country "
        "code prices a stay through a residential connection in that country, "
        "so the result is what a shopper resident there would be quoted."
    ),
    SEARCH_PARAM_NOUNS=(
        "Origins, destinations, property names, dates, guest and passenger "
        "counts, cabin class, currency, price limits, airline filters and "
        "property filters"
    ),
    RESULT_NOUN="Flight and hotel results",
    RESULT_SENTENCE=(
        "Fares and room rates are returned to you and not retained. Both go "
        "stale within minutes, so nothing is cached or reused."
    ),
    COST_SENTENCE=(
        "Cost of a flight fan-out is roughly one billed upstream request per "
        "date/destination combination searched, and a hotel call is exactly "
        "one"
    ),
    BRAND_NOUNS="Airline, airport, property and travel-brand names",
    STALENESS_BULLET=(
        "**Fares and room rates change constantly and go stale within "
        "minutes.** Every result is a snapshot of the moment it was fetched. "
        "Do not cache a price, do not reuse an earlier result, and do not "
        "present a previously fetched price as current. If you display a "
        "price, display when it was fetched."
    ),
    CONFIRMATION_SITES="the airline, property or booking site",
    EMPTY_RESULT_BULLET=(
        "**An empty result set is a valid answer**, meaning nothing matched "
        "the route or destination, the dates and any filters applied. It is "
        "not an error."
    ),
    PRICE_CONTEXT_BULLET=(
        PRODUCT_CONTEXT["flights"]["PRICE_CONTEXT_BULLET"]
        + " "
        + PRODUCT_CONTEXT["hotels"]["PRICE_CONTEXT_BULLET"]
    ),
)


def apply_context(source: str, products: str) -> str:
    """Replace {{TOKEN}} placeholders for this deployment.

    An unknown product falls back to flights rather than leaving raw
    placeholders on a public page.
    """
    ctx = PRODUCT_CONTEXT.get(products) or PRODUCT_CONTEXT["flights"]
    for key, value in ctx.items():
        source = source.replace("{{" + key + "}}", value)
    return source


@lru_cache(maxsize=8)
def render_document(name: str, products: str = "flights") -> str | None:
    """Render `legal/<name>.md`. None when the file is absent, so a missing
    document is a 404 rather than a blank page that looks like a policy."""
    path = LEGAL_DIR / f"{name}.md"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    source = apply_context(source, products)
    first = next(
        (ln.lstrip("# ").strip() for ln in source.splitlines() if ln.startswith("#")),
        name.title(),
    )
    return page(first, markdown_to_html(source))


SUPPORT_MD = """# Support — {{PRODUCT}}

**Service:** `https://{{HOST}}/mcp`

## Contact

Email **mtnrabi@gmail.com**. Include the tool you called, the arguments, and
the UTC time of the call. There is no account to look up — the server stores no
user identity — so those three things are what make a report actionable.

## Before you write

- **"No RapidAPI key was supplied"** — the key is not reaching the server.
  Supply it as an `x-rapidapi-key` header, as `?rapidapi_key=` on the server
  URL, or in your client's API-key field. Get one at `{{SIGNUP_URL}}`.
- **"not subscribed to this API"** — the key is valid but has no subscription
  to this specific API. Subscribing to the free tier fixes it.
- **Out of requests** — the plan's quota is spent. Every search combination is
  one billed request; `api_usage` in each response reports what a call cost and
  what remains.
- **An empty result** — a valid answer, not an error. Some searches genuinely
  return nothing; try different dates or a nearby location.

## Status

Health and current configuration: `https://{{HOST}}/health`

## Non-affiliation

This is an independent service returning publicly available {{DATA_NOUN}}
pricing. It is not affiliated with, endorsed by, or sponsored by
{{NOT_AFFILIATED}}.
"""


def support_html(products: str = "flights") -> str:
    """Support page for this deployment.

    A support page that names the wrong product sends a paying customer to the
    wrong RapidAPI listing to fix their key.
    """
    ctx = PRODUCT_CONTEXT.get(products) or PRODUCT_CONTEXT["flights"]
    md = apply_context(SUPPORT_MD, products)
    return page(f"Support — {ctx['PRODUCT']}", markdown_to_html(md))



INDEX_MD = """# {{PRODUCT}}

A hosted Model Context Protocol server returning real-time {{DATA_NOUN}} data.
**No advertising, no sponsored content, no paid placement** anywhere in a tool
result.

## Connect
{{SIGN_IN_BLOCK}}
**Or bring your own RapidAPI key:** `{{MCP_URL}}` — transport: streamable HTTP.

Every search is billed to the caller's own RapidAPI subscription, supplied per
request as an `x-rapidapi-key` header. This server holds no upstream
credential of its own and never stores a key sent on a request. Get a key, free
tier available, at `{{SIGNUP_URL}}`.

## Tools

{{TOOL_BULLETS}}

Both are read-only. They cannot book, hold, pay for or cancel anything.

## Policies and contact

- Privacy policy: `{{SITE}}/privacy`
- Terms of service: `{{SITE}}/terms`
- Support: `{{SITE}}/support` — mtnrabi@gmail.com
- Health and current configuration: `{{SITE}}/health`

## Non-affiliation

This is an independent service returning publicly available {{DATA_NOUN}}
pricing. It is not affiliated with, endorsed by, or sponsored by
{{NOT_AFFILIATED}}.
"""


SIGN_IN_MD = """
**Sign in, no key to paste:** `{{OAUTH_URL}}`, transport: streamable HTTP.

Works in clients that support MCP authorization: a Sign in button appears, you
sign in with Google, and you paste your RapidAPI key once on the `{{SITE}}/connect`
page. Nothing goes in the client config.
"""


def index_html(
    products: str, site: str, signup_url: str, oauth_url: str | None = None
) -> str:
    """The landing page at `/`.

    Deliberately a summary of facts a reviewer checks -- transport, auth model,
    tool list, the four policy links, the ad-free statement -- rather than
    marketing copy. The one page on this domain that is read by a person
    deciding whether to approve the listing should answer their checklist.
    """
    ctx = PRODUCT_CONTEXT.get(products) or PRODUCT_CONTEXT["flights"]
    md = apply_context(INDEX_MD, products)
    # The sign-in paragraph is dropped, not greyed out, on a deployment where
    # `/mcp/oauth` is not registered. `/health` reports the same fact as
    # `oauth_enabled`; a landing page that advertises a 404 is worse than a
    # landing page that says nothing.
    md = md.replace("{{SIGN_IN_BLOCK}}", SIGN_IN_MD if oauth_url else "")
    md = (
        md.replace("{{SITE}}", site)
        .replace("{{MCP_URL}}", f"{site}/mcp")
        .replace("{{OAUTH_URL}}", oauth_url or "")
        .replace("{{SIGNUP_URL}}", signup_url)
    )
    return page(ctx["PRODUCT"], markdown_to_html(md))
