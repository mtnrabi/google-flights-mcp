"""
The checks an app-directory reviewer runs, run in CI instead.

Two directories gate this server, and both publish their criteria:

* Anthropic Software Directory Policy §5.E -- "MCP servers must provide all
  applicable annotations for their tools, in particular readOnlyHint,
  destructiveHint, and title." §3.A requires a privacy policy that resolves;
  "Missing or incomplete privacy policies result in immediate rejection."
* OpenAI plugin submission -- "readOnlyHint, openWorldHint, and destructiveHint
  values for every MCP tool", and it names missing or incorrect tool metadata
  as a common cause of rejection.

Every assertion below is one of those criteria. They exist because the failure
mode is silent: the server keeps working perfectly while the schema it
advertises quietly stops being reviewable -- a parameter added without a
docstring line, an annotation dropped in a refactor, `idempotentHint` flipped
to True by someone who read "read-only" and assumed it followed.

`idempotentHint` is the one worth spelling out. These tools return live fares
that move minute to minute. A host that reads `idempotentHint: True` is
entitled to serve a cached answer to a repeated call, which means quoting a
stale price to someone about to book. It must stay False on every search tool
here, and this file is what keeps it False.
"""

import json

import httpx
import pytest

from src.schema_docs import (
    document_params,
    parse_args_section,
    undocumented_params,
)
from tests.test_server import build_with_upstream

# Every deployment, because the tools differ per product and a hotels-only
# listing is reviewed on its own schema.
PRODUCTS = ("flights", "hotels", "both")


def tools_for(products: str):
    mcp = build_with_upstream(
        lambda _r: httpx.Response(200, json=[]), products=products
    )
    import anyio

    async def _list():
        return await mcp._list_tools()

    return [json.loads(t.to_mcp_tool().model_dump_json()) for t in anyio.run(_list)]


class TestToolAnnotations:
    """Anthropic §5.E and OpenAI's submission checklist."""

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_every_tool_has_title_and_annotations(self, products):
        tools = tools_for(products)
        assert tools, f"{products} deployment exposes no tools"
        for tool in tools:
            assert tool.get("title"), f"{tool['name']} has no title"
            annotations = tool.get("annotations")
            assert annotations, f"{tool['name']} has no annotations"
            for hint in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                assert hint in annotations, f"{tool['name']} is missing {hint}"
            assert annotations.get("title"), f"{tool['name']} annotation title"

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_search_tools_are_read_only_and_non_destructive(self, products):
        for tool in tools_for(products):
            annotations = tool["annotations"]
            assert annotations["readOnlyHint"] is True, tool["name"]
            assert annotations["destructiveHint"] is False, tool["name"]
            # These reach a live third-party API whose result set is not a
            # closed domain.
            assert annotations["openWorldHint"] is True, tool["name"]

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_idempotent_hint_is_false_everywhere(self, products):
        """Fares change between two identical calls. A host that caches one
        because the tool claimed idempotence quotes a stale price."""
        for tool in tools_for(products):
            assert tool["annotations"].get("idempotentHint") is False, (
                f"{tool['name']} must declare idempotentHint False: its result "
                "is a live price, not a stable lookup"
            )

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_tool_names_are_within_the_64_character_limit(self, products):
        """Anthropic §5.C: 'MCP tool names must not exceed 64 characters.'"""
        for tool in tools_for(products):
            assert len(tool["name"]) <= 64, tool["name"]

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_every_tool_describes_itself(self, products):
        for tool in tools_for(products):
            assert len(tool.get("description") or "") > 100, tool["name"]


class TestParameterDescriptions:
    """A schema is what a model plans a call from. `{"type": "integer"}` for
    `seat_type` is not enough for it to choose 3 for business class."""

    @pytest.mark.parametrize("products", PRODUCTS)
    def test_no_parameter_ships_without_a_description(self, products):
        for tool in tools_for(products):
            missing = undocumented_params(tool["inputSchema"])
            assert not missing, (
                f"{tool['name']} exposes undocumented parameters: {missing}. "
                "Add a line for each to the function's Args: docstring block."
            )

    def test_the_descriptions_are_the_docstring_text(self):
        """Not just non-empty -- actually the sentence from the source."""
        tool = next(
            t for t in tools_for("flights") if t["name"] == "search_oneway_flights"
        )
        properties = tool["inputSchema"]["properties"]
        assert "1 economy" in properties["seat_type"]["description"]
        assert "IATA" in properties["from_airport"]["description"]
        # A wrapped multi-line entry arrives as one sentence, not with the
        # source's line breaks in it.
        assert "\n" not in properties["max_searches"]["description"]
        assert "sampled evenly" in properties["max_searches"]["description"]


class TestPublicPages:
    """Anthropic §3.A/§3.B and OpenAI's URL requirements: policy, terms and
    support must resolve for an anonymous visitor, and the base URL a reviewer
    types must not be a 404."""

    def _transport(self, products="flights"):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[]), products=products
        )
        return httpx.ASGITransport(app=mcp.http_app(stateless_http=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/privacy", "/terms", "/support"])
    async def test_page_resolves_anonymously(self, path):
        async with httpx.AsyncClient(
            transport=self._transport(), base_url="http://test"
        ) as client:
            response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")

    @pytest.mark.asyncio
    async def test_landing_page_links_every_policy(self):
        async with httpx.AsyncClient(
            transport=self._transport(), base_url="http://test"
        ) as client:
            body = (await client.get("/")).text
        for fragment in ("/privacy", "/terms", "/support", "/health", "/mcp"):
            assert fragment in body, f"landing page never mentions {fragment}"

    @pytest.mark.asyncio
    async def test_landing_page_states_the_ad_free_position(self):
        """Anthropic §4.C bars 'Software that serves advertisements, sponsored
        content, paid product placements'. This deployment carries none, and
        says so on the page a reviewer opens first."""
        async with httpx.AsyncClient(
            transport=self._transport(), base_url="http://test"
        ) as client:
            body = (await client.get("/")).text.lower()
        assert "no advertising" in body
        assert "sponsored" in body

    @pytest.mark.asyncio
    async def test_health_publishes_the_policy_urls(self):
        async with httpx.AsyncClient(
            transport=self._transport(), base_url="http://test"
        ) as client:
            payload = (await client.get("/health")).json()
        assert payload["privacy_url"] == "https://mcp.test/privacy"
        assert payload["terms_url"] == "https://mcp.test/terms"
        assert payload["support_url"] == "https://mcp.test/support"
        assert payload["contact_email"]
        assert payload["ads"] is False

    @pytest.mark.asyncio
    async def test_hotels_landing_page_never_names_google(self):
        """Same trap as the policy documents: a landing page naming the wrong
        upstream is the first thing a reviewer of that listing reads."""
        async with httpx.AsyncClient(
            transport=self._transport("hotels"), base_url="http://test"
        ) as client:
            body = (await client.get("/")).text
        assert "Google" not in body
        assert "search_oneway_flights" not in body


class TestSiteOrigin:
    def test_origin_is_derived_from_the_mcp_url(self):
        from tests.test_server import make_settings

        settings = make_settings(public_url="https://mcp.test/mcp")
        assert settings.site_origin() == "https://mcp.test"

    def test_a_url_without_a_scheme_still_yields_something_usable(self):
        from tests.test_server import make_settings

        settings = make_settings(public_url="mcp.test/mcp")
        assert settings.site_origin() == "mcp.test"


class TestArgsParser:
    """The parser itself, at the edges that would silently drop a description."""

    def test_reads_a_plain_block(self):
        docs = parse_args_section(
            """
            Args:
                a: First.
                b: Second.
            """
        )
        assert docs == {"a": "First.", "b": "Second."}

    def test_folds_continuation_lines(self):
        docs = parse_args_section(
            """
            Args:
                a: A description that runs
                    across two lines.
            """
        )
        assert docs == {"a": "A description that runs across two lines."}

    def test_stops_at_the_next_section(self):
        docs = parse_args_section(
            """
            Args:
                a: First.

            Returns:
                Something that is not a parameter.
            """
        )
        assert docs == {"a": "First."}

    def test_ignores_a_summary_above_the_block(self):
        docs = parse_args_section(
            """Do a thing.

            This paragraph mentions a: not a parameter.

            Args:
                a: First.
            """
        )
        assert docs == {"a": "First."}

    def test_tolerates_a_parenthesised_type(self):
        assert parse_args_section("Args:\n    a (int): First.") == {"a": "First."}

    def test_no_block_and_no_docstring_are_both_empty(self):
        assert parse_args_section(None) == {}
        assert parse_args_section("Just a summary.") == {}

    def test_decorator_is_a_no_op_without_a_block(self):
        def fn(a: int) -> int:
            """No Args here."""
            return a

        before = dict(fn.__annotations__)
        assert document_params(fn).__annotations__ == before

    def test_decorator_leaves_an_existing_annotated_alone(self):
        """A second Field() inside one Annotated is a pydantic error, not a
        merge, so an explicitly annotated parameter must be skipped."""
        from typing import Annotated, get_args

        from pydantic import Field

        def fn(a: Annotated[int, Field(description="explicit")]) -> int:
            """
            Args:
                a: docstring version.
            """
            return a

        document_params(fn)
        metadata = get_args(fn.__annotations__["a"])[1:]
        assert len(metadata) == 1
        assert metadata[0].description == "explicit"
