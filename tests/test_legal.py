"""
The public policy pages.

Two classes of risk here, and they are different.

The renderer supports a closed subset of Markdown. That is safe only while the
source documents stay inside it, so `TestSourceStaysInTheSubset` asserts the
documents use nothing else. If someone adds a link or a code fence to the
privacy policy, that test fails here rather than the construct appearing as
literal `[text](url)` on a page an app-store reviewer is reading.

The rest is about what a reviewer actually checks: that the URLs resolve
anonymously, that the pages say what the server really does, and that the
non-affiliation line is present.
"""

import re
from pathlib import Path

import httpx
import pytest
from fastmcp import Client  # noqa: F401  (import parity with the other suites)

from src.legal import (
    LEGAL_DIR,
    support_html,
    markdown_to_html,
    render_document,
)
from tests.test_server import build_with_upstream

DOCS = ("privacy", "terms")


class TestSourceStaysInTheSubset:
    """The renderer handles headings, tables, bullets, bold, inline code and
    rules. Nothing else. These tests are the tripwire."""

    @pytest.mark.parametrize("name", DOCS)
    def test_document_exists(self, name):
        assert (LEGAL_DIR / f"{name}.md").is_file()

    @pytest.mark.parametrize("name", DOCS)
    def test_no_code_fences(self, name):
        assert "```" not in (LEGAL_DIR / f"{name}.md").read_text()

    @pytest.mark.parametrize("name", DOCS)
    def test_no_markdown_links(self, name):
        text = (LEGAL_DIR / f"{name}.md").read_text()
        assert not re.search(r"\[[^\]]+\]\([^)]+\)", text)

    @pytest.mark.parametrize("name", DOCS)
    def test_no_ordered_lists(self, name):
        text = (LEGAL_DIR / f"{name}.md").read_text()
        assert not re.search(r"^\s*\d+\.\s", text, re.M)


class TestRenderer:
    def test_heading(self):
        assert "<h2>Scope</h2>" in markdown_to_html("## Scope")

    def test_bullets_group_into_one_list(self):
        out = markdown_to_html("- one\n- two")
        assert out.count("<ul>") == 1
        assert out.count("<li>") == 2

    def test_table(self):
        out = markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
        assert "<th>A</th>" in out and "<td>2</td>" in out
        assert "|---|" not in out

    def test_inline_code_and_bold(self):
        out = markdown_to_html("Use `x-rapidapi-key` and **never** log it.")
        assert "<code>x-rapidapi-key</code>" in out
        assert "<strong>never</strong>" in out

    def test_horizontal_rule(self):
        assert "<hr>" in markdown_to_html("a\n\n---\n\nb")

    def test_html_in_source_is_escaped_not_executed(self):
        """The policy text is trusted, but escaping before interpolation is the
        habit that survives the day some of it is not."""
        out = markdown_to_html("A <script>alert(1)</script> B")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_paragraph_lines_join(self):
        out = markdown_to_html("one line\nsecond line")
        assert out.count("<p>") == 1

    def test_unknown_construct_survives_as_text(self):
        """Content must never be silently dropped from a legal page."""
        assert "important clause" in markdown_to_html("> important clause")


class TestRenderedDocuments:
    @pytest.mark.parametrize("name", DOCS)
    def test_renders(self, name):
        out = render_document(name)
        assert out and out.startswith("<!doctype html>")
        assert "<h1>" in out

    @pytest.mark.parametrize("name", DOCS)
    def test_no_unrendered_markdown_leaks_through(self, name):
        out = render_document(name)
        # A stray pipe row or hash heading means the renderer missed something
        # a reviewer would see as raw markup.
        assert not re.search(r"^\s*\|", out, re.M)
        assert not re.search(r"^\s*#{1,6}\s", out, re.M)

    def test_missing_document_is_none_not_blank(self):
        assert render_document("no-such-policy") is None

    def test_privacy_states_what_is_not_collected(self):
        out = render_document("privacy").lower()
        assert "key" in out
        assert "not" in out

    @pytest.mark.parametrize("name", DOCS)
    def test_non_affiliation_present(self, name):
        assert "not affiliated" in render_document(name).lower()

    def test_support_page_gives_a_contact(self):
        assert "mtnrabi@gmail.com" in support_html()
        assert "not affiliated" in support_html().lower()


class TestRoutesAreAnonymouslyReachable:
    """A reviewer fetches these with no credentials. If any of them ever needs
    auth, the submission is rejected."""

    def _client(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]))
        return httpx.ASGITransport(app=mcp.http_app(stateless_http=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/privacy", "/terms", "/support"])
    async def test_reachable_without_any_header(self, path):
        transport = self._client()
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r = await client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<!doctype html>" in r.text


class TestPerProductDocuments:
    """One set of documents, three deployments.

    Serving the flights privacy policy from the hotels host is not cosmetic: a
    policy naming the wrong service and the wrong upstream processor is not a
    policy for that service, and it is the first thing a store reviewer reads.
    mrabi caught this in production on 2026-08-18.
    """

    def test_hotels_documents_never_mention_flights(self):
        from src.legal import render_document, support_html

        for doc in (
            render_document("privacy", "hotels"),
            render_document("terms", "hotels"),
            support_html("hotels"),
        ):
            assert doc
            assert "Google" not in doc, "hotels docs must not name Google at all"
            assert "search_oneway_flights" not in doc

    def test_flights_documents_never_mention_hotels(self):
        from src.legal import render_document, support_html

        for doc in (
            render_document("privacy", "flights"),
            render_document("terms", "flights"),
            support_html("flights"),
        ):
            assert doc
            assert "Booking" not in doc, "flights docs must not name Booking at all"
            assert "search_hotels" not in doc

    def test_each_names_its_own_host_and_signup(self):
        from src.legal import render_document, support_html

        assert "hotels.flightpowers.com" in render_document("privacy", "hotels")
        assert "flights.flightpowers.com" in render_document("privacy", "flights")
        assert "booking-live-api" in support_html("hotels")
        assert "google-flights-live-api" in support_html("flights")

    def test_no_placeholder_survives_to_a_public_page(self):
        """A raw {{TOKEN}} on a policy page is worse than the wrong product --
        it reads as an unfinished site."""
        from src.legal import render_document, support_html

        for products in ("flights", "hotels", "both"):
            for doc in (
                render_document("privacy", products),
                render_document("terms", products),
                support_html(products),
            ):
                assert "{{" not in doc, products
