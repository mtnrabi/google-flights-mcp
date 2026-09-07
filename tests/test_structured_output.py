"""The MCP structured-output contract: `outputSchema`, `structuredContent`, `isError`.

These tools always returned a JSON object and always carried a
`search_status` field. What they did not do was say so in the protocol. That
gap is what this file closes, and what it now guards.

Two spec mechanisms are involved, both read from the current revision,
**2026-07-28** (structured output itself landed in 2025-06-18, "Add support
for structured tool output", and is unchanged in substance since):

* `outputSchema` on the tool, `structuredContent` on the result.
  "Servers **MUST** provide structured results that conform to this schema.
  Clients **SHOULD** validate structured results against this schema."
  The MUST is why `test_every_exit_path_conforms_to_its_declared_schema`
  exists and why the schemas are permissive: these tools have several
  legitimate non-search exits, and a schema that forbade one of them would
  make the server non-conformant on a path we ship on purpose.

* `isError: true` for a failed call. The spec puts "API failures" under
  Tool Execution Errors and says clients "**SHOULD** provide tool execution
  errors to language models to enable self-correction". Nothing in the spec
  obliges a host to show `structuredContent` to the model at all -- so a
  failure that lives only in the payload is a failure the model may never
  learn about. `search_status: "degraded"` was exactly that.

The backwards-compatibility duplicate is not optional for us:

  "For backwards compatibility, a tool that returns structured content
   SHOULD also return the serialized JSON in a TextContent block."

We have callers on clients that predate structured content, so the text
block is load-bearing rather than ceremonial, and it is asserted on both the
success and the error path below.
"""

import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from src.output_schema import (
    FLIGHTS_OUTPUT_SCHEMA,
    HOTELS_OUTPUT_SCHEMA,
    SEARCH_STATUS_VALUES,
)
from tests.test_server import (
    ONEWAY_ROW,
    build_with_upstream,
    call,
    call_result,
)

FLIGHT_TOOLS = ("search_oneway_flights", "search_roundtrip_flights")
HOTEL_TOOLS = ("search_hotels", "find_hotel_by_name")

# The schema FastMCP infers from a bare `-> dict[str, Any]` annotation. It is
# a valid schema and tells a client nothing, which is the state this work
# replaced; if a tool ever falls back to it, the declaration was dropped.
INFERRED_PLACEHOLDER = {"type": "object", "additionalProperties": True}

ONEWAY_ARGS = dict(from_airport="TLV", to_airport="BUD", departure_date="2026-09-20")
ROUNDTRIP_ARGS = dict(**ONEWAY_ARGS, return_date="2026-09-27")
HOTEL_ARGS = dict(destination="Rome", checkin_date="2026-09-20", checkout_date="2026-09-22")


def _tools(products="both"):
    mcp = build_with_upstream(lambda _r: httpx.Response(200, json=[]), products=products)

    import anyio

    async def _list():
        return await mcp._list_tools()

    return {
        t.name: json.loads(t.to_mcp_tool().model_dump_json())
        for t in anyio.run(_list)
    }


def _flights(rows, status=None, reason=None):
    headers = {}
    if status:
        headers["X-Search-Status"] = status
    if reason:
        headers["X-Search-Reason"] = reason
    return build_with_upstream(
        lambda _r: httpx.Response(200, json=rows, headers=headers)
    )


class TestTheSchemaIsDeclaredAndReal:
    """`tools/list` is where a client learns the result shape."""

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_every_tool_declares_an_output_schema(self, name):
        schema = _tools()[name].get("outputSchema")
        assert schema, f"{name} declares no outputSchema"
        assert schema != INFERRED_PLACEHOLDER, (
            f"{name} fell back to the schema FastMCP infers from "
            "`-> dict[str, Any]`, which declares nothing"
        )
        assert schema.get("properties"), f"{name} declares no properties"

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_every_declared_schema_is_itself_valid(self, name):
        # A malformed schema is not a test failure at import time -- FastMCP
        # accepts any object schema -- so it would ship and only break the
        # clients that actually validate.
        Draft202012Validator.check_schema(_tools()[name]["outputSchema"])

    @pytest.mark.parametrize("name", FLIGHT_TOOLS)
    def test_flight_tools_publish_the_search_status_vocabulary(self, name):
        status = _tools()[name]["outputSchema"]["properties"]["search_status"]
        assert status["enum"] == list(SEARCH_STATUS_VALUES)
        assert set(status["enum"]) == {"ok", "empty", "partial", "degraded"}
        # The whole point of publishing it: a client can be told what an
        # empty array means without reading our documentation.
        assert "empty" in status["description"]

    @pytest.mark.parametrize("name", HOTEL_TOOLS)
    def test_hotel_tools_claim_no_search_status(self, name):
        # The hotels upstream sends no X-Search-Status header, so there is no
        # honest value to put here. Declaring the field anyway would promise
        # a signal that never arrives.
        assert "search_status" not in _tools()[name]["outputSchema"]["properties"]

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_unknown_keys_are_permitted(self, name):
        # `additionalProperties: false` would be the stricter-looking choice
        # and would break the first time anything appended a key -- an
        # upstream field echoed through, or a future envelope addition. The
        # spec's MUST is on us, not on the client.
        assert _tools()[name]["outputSchema"]["additionalProperties"] is True

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_only_results_is_required(self, name):
        # `results` is the one key every exit path carries. Requiring
        # `result_count` or `api_usage` would be false for the keyless reply,
        # and a server that requires a key it does not always send violates
        # "servers MUST provide structured results that conform".
        assert _tools()[name]["outputSchema"]["required"] == ["results"]

    def test_the_schema_survives_the_per_product_deployments(self):
        # Every tool is constructed on both deployments and the unwanted ones
        # are removed afterwards, so a broken schema on the hotel tools would
        # take the flights deployment's cold start down with it.
        for products, expected in (
            ("flights", set(FLIGHT_TOOLS)),
            ("hotels", set(HOTEL_TOOLS)),
        ):
            tools = _tools(products)
            assert set(tools) == expected
            for tool in tools.values():
                assert tool["outputSchema"]["properties"]


class TestDegradedIsAnError:
    """The design decision this file exists to pin down.

    A degraded search means every combination failed: there is no data, and
    an empty list is not an answer. That is a tool execution error, and the
    spec's channel for one is `isError`, which clients SHOULD pass to the
    model. `search_status` alone depended on a host choosing to surface
    `structuredContent`, which the spec never requires.
    """

    @pytest.mark.asyncio
    async def test_a_degraded_search_sets_is_error(self):
        result = await call_result(
            _flights([], status="degraded", reason="blocked_page"),
            "search_oneway_flights",
            **ONEWAY_ARGS,
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_a_degraded_search_keeps_its_payload(self):
        """`isError` is a flag, not a reason to throw the result away.

        `api_usage` is the part that must not be lost: a degraded search
        still spent the caller's own RapidAPI requests, and raising a bare
        ToolError instead would hide a charge they have to pay.
        """
        result = await call_result(
            _flights([], status="degraded", reason="blocked_page"),
            "search_oneway_flights",
            **ONEWAY_ARGS,
        )
        payload = result.structured_content
        assert payload is not None, "the error dropped its structured content"
        assert payload["search_status"] == "degraded"
        assert payload["results"] == []
        assert payload["api_usage"]["requests_used_by_this_call"] == 1
        assert payload["search_coverage"]["searched_combinations"] == 1
        assert "blocked_page" in payload["message"]

    @pytest.mark.asyncio
    async def test_a_degraded_error_still_serializes_json_into_a_text_block(self):
        """The backwards-compatibility duplicate, on the error path too.

        Callers on clients that predate structured content read `content[]`
        and nothing else. An error that reached them as an empty content list
        would be an error they cannot read.

        Since 2026-09-02 the JSON is the *second* block, not the first: the
        first is the plain-language warning (tests/test_loud_status.py). The
        duplicate is unchanged, it just no longer leads. A caller that took
        `content[0]` and parsed it now wants `content[-1]`, which is why this
        asserts on the last block rather than the count alone.
        """
        result = await call_result(
            _flights([], status="degraded"), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert len(result.content) == 2
        assert not result.content[0].text.startswith("{"), "prose first"
        text = result.content[-1].text
        assert json.loads(text) == result.structured_content

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_behaves_the_same_way(self):
        result = await call_result(
            _flights([], status="degraded"), "search_roundtrip_flights", **ROUNDTRIP_ARGS
        )
        assert result.is_error
        assert result.structured_content["search_status"] == "degraded"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,rows",
        [
            ("ok", [ONEWAY_ROW]),
            ("empty", []),
            ("partial", [ONEWAY_ROW]),
        ],
    )
    async def test_nothing_else_is_flagged_as_an_error(self, status, rows):
        """Only `degraded` is a failure.

        `empty` is a true negative -- the search ran and Google has nothing --
        and `partial` carries results the caller can use. Flagging either
        would throw away a real answer over a caveat, and would train a model
        to treat "no flights on that date" as a fault to retry.
        """
        result = await call_result(
            _flights(rows, status=status), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert not result.is_error
        assert result.structured_content["search_status"] == status

    @pytest.mark.asyncio
    async def test_a_silent_backend_is_not_an_error(self):
        # No header at all: a backend that predates it, or a hop that dropped
        # it. We do not know the search failed, so we must not claim it did.
        result = await call_result(
            _flights([]), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert not result.is_error
        assert "search_status" not in result.structured_content


class TestSuccessResultsAreUnchangedOnTheWire:
    """Declaring a schema must not have moved anything a caller already reads."""

    @pytest.mark.asyncio
    async def test_a_successful_result_still_carries_both_representations(self):
        result = await call_result(
            _flights([ONEWAY_ROW], status="ok"), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert not result.is_error
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == result.structured_content
        assert result.structured_content["results"][0]["buy_link"]


class TestEveryExitPathConformsToItsDeclaredSchema:
    """The spec's MUST, checked against real payloads rather than by eye.

    Each of these is a shape the tools genuinely return in production. A
    schema that rejected any of them would make the server non-conformant on
    a path it ships deliberately -- which is the failure mode a tighter,
    better-looking schema would have introduced silently.
    """

    @staticmethod
    def _check(payload, schema):
        errors = sorted(
            Draft202012Validator(schema).iter_errors(payload), key=lambda e: e.path
        )
        assert not errors, "; ".join(
            f"{list(e.path)}: {e.message}" for e in errors
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,rows",
        [("ok", [ONEWAY_ROW]), ("empty", []), ("partial", [ONEWAY_ROW])],
    )
    async def test_flight_search_results(self, status, rows):
        out = await call(_flights(rows, status=status), "search_oneway_flights", **ONEWAY_ARGS)
        self._check(out, FLIGHTS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_a_degraded_result(self):
        result = await call_result(
            _flights([], status="degraded"), "search_oneway_flights", **ONEWAY_ARGS
        )
        self._check(result.structured_content, FLIGHTS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_the_keyless_reply(self):
        # Returned as data rather than raised, because the model has to relay
        # the signup instructions to a human.
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json=[ONEWAY_ROW]), fallback_rapidapi_key=""
        )
        out = await call(mcp, "search_oneway_flights", **ONEWAY_ARGS)
        assert out["needs_api_key"] is True
        self._check(out, FLIGHTS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_a_hotel_search_result(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(
                200, json={"properties": [{"name": "Hotel Roma", "price": 120}]}
            )
        )
        out = await call(mcp, "search_hotels", **HOTEL_ARGS)
        self._check(out, HOTELS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_a_hotel_reply_with_no_key(self):
        mcp = build_with_upstream(
            lambda _r: httpx.Response(200, json={"properties": []}),
            fallback_rapidapi_key="",
        )
        out = await call(mcp, "search_hotels", **HOTEL_ARGS)
        assert out["needs_api_key"] is True
        self._check(out, HOTELS_OUTPUT_SCHEMA)


class TestDirectoryMetadataSurvived:
    """Adding a schema must not have disturbed what the directories gate on.

    Anthropic §5.E and OpenAI's submission checklist both read these, and
    `idempotentHint` in particular: these tools return live fares, so a host
    entitled to cache a repeated call would quote a stale price to someone
    about to book.
    """

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_title_and_annotations_are_intact(self, name):
        tool = _tools()[name]
        assert tool["title"]
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is True
        assert annotations["idempotentHint"] is False
