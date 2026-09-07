"""The first line of the text block, pinned per status.

`search_status` has been declared, schema'd and (for `degraded`) carried on
`isError` since 2026-08-27. All of that is aimed at the client. The model
usually sees one thing -- the text block -- and that block was the serialized
JSON and nothing else, so the status sat mid-object next to `"currency"`.

u/lulu_dev put it on the r/mcp thread (comment p7exhmy, 2026-09-02): "make
the degraded case read as alarming in plain language, not just present as a
quiet field ... Schema for the clients that read it, loud natural language for
the ones that don't." This file is what we promised him, held in place.

The case that matters most is `partial`, not `degraded`. A degraded result
carries `isError: true`, so a spec-following host has something to flag.
A partial result has real rows and `isError` is false on purpose, because
those rows are usable -- so nothing in the protocol stops a model reading the
rows it got as the whole answer while part of the requested range was never
scraped. Hence the coverage line, and hence the insistence that it NAMES the
missing combinations rather than hinting at them.
"""

import json

import httpx
import pytest

from src.status_text import DEGRADED_FIRST_LINE, MAX_NAMED_COMBINATIONS
from tests.test_server import ONEWAY_ROW, build_with_upstream, call_result

ONEWAY_ARGS = dict(from_airport="TLV", to_airport="BUD", departure_date="2026-09-20")
RANGE_ARGS = dict(
    from_airport="TLV",
    to_airport="BUD",
    departure_date_from="2026-09-20",
    departure_date_to="2026-09-22",
)


def _row(date):
    return dict(ONEWAY_ROW, departure_date=date, buy_link=f"https://google.test/{date}")


def _per_date(behaviour):
    """An upstream that answers differently per requested departure_date.

    `behaviour` maps a date to ("ok" | "partial" | "degraded" | "boom").
    Anything not listed answers a healthy single row.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        date = json.loads(request.content)["departure_date"]
        kind = behaviour.get(date, "ok")
        if kind == "boom":
            return httpx.Response(500, json={"message": "upstream exploded"})
        headers = {"X-Search-Status": "ok" if kind == "ok" else kind}
        rows = [] if kind in ("degraded", "partial") else [_row(date)]
        return httpx.Response(200, json=rows, headers=headers)

    return build_with_upstream(handler)


def _first_line(result):
    return result.content[0].text


def _json_block(result):
    return json.loads(result.content[-1].text)


class TestDegradedReadsAsAlarming:
    @pytest.mark.asyncio
    async def test_the_first_line_is_prose_not_json(self):
        result = await call_result(
            _per_date({"2026-09-20": "degraded"}),
            "search_oneway_flights",
            **ONEWAY_ARGS,
        )
        assert _first_line(result) == DEGRADED_FIRST_LINE
        assert _first_line(result).startswith("WARNING: this search did not complete.")
        assert "do not tell the user whether flights exist" in _first_line(result)

    @pytest.mark.asyncio
    async def test_the_degraded_line_contains_no_digits(self):
        """Rule 1: nothing interpolated, so no invented metric can drift in.

        A count, a latency or a percentage in a fixed warning is a number a
        model will repeat as fact. There is nothing to get wrong here because
        there is nothing to substitute.
        """
        assert not any(ch.isdigit() for ch in DEGRADED_FIRST_LINE)

    @pytest.mark.asyncio
    async def test_the_json_still_follows_and_still_matches(self):
        """The backwards-compatibility duplicate survives, just second."""
        result = await call_result(
            _per_date({"2026-09-20": "degraded"}),
            "search_oneway_flights",
            **ONEWAY_ARGS,
        )
        assert len(result.content) == 2
        assert _json_block(result) == result.structured_content
        assert result.structured_content["search_status"] == "degraded"
        assert result.is_error

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_says_the_same_thing(self):
        result = await call_result(
            _per_date({"2026-09-20": "degraded"}),
            "search_roundtrip_flights",
            from_airport="TLV",
            to_airport="BUD",
            departure_date="2026-09-20",
            return_date="2026-09-27",
        )
        assert _first_line(result) == DEGRADED_FIRST_LINE


class TestPartialNamesWhatIsMissing:
    """The dangerous status: real rows, `isError` false, part of the range gone."""

    @pytest.mark.asyncio
    async def test_the_coverage_line_names_the_missing_combinations(self):
        result = await call_result(
            _per_date({"2026-09-21": "partial"}),
            "search_oneway_flights",
            **RANGE_ARGS,
        )
        line = _first_line(result)
        assert line.startswith("COVERAGE WARNING: 2 of 3 searches completed.")
        # The point of the whole exercise: the missing date is NAMED.
        assert "2026-09-21 to BUD" in line
        assert "2026-09-20" not in line and "2026-09-22" not in line
        assert "floor on what is available" in line

    @pytest.mark.asyncio
    async def test_the_counts_come_from_the_real_outcome(self):
        """Both numbers are countable from the payload that ships beside them."""
        result = await call_result(
            _per_date({"2026-09-20": "partial", "2026-09-22": "partial"}),
            "search_oneway_flights",
            **RANGE_ARGS,
        )
        payload = _json_block(result)
        attempted = payload["search_coverage"]["searched_combinations"]
        assert attempted == 3
        assert _first_line(result).startswith(
            f"COVERAGE WARNING: 1 of {attempted} searches completed."
        )
        assert "2026-09-20 to BUD" in _first_line(result)
        assert "2026-09-22 to BUD" in _first_line(result)

    @pytest.mark.asyncio
    async def test_partial_is_still_not_an_error_and_still_carries_its_rows(self):
        result = await call_result(
            _per_date({"2026-09-21": "partial"}),
            "search_oneway_flights",
            **RANGE_ARGS,
        )
        assert not result.is_error
        assert result.structured_content["search_status"] == "partial"
        assert result.structured_content["result_count"] == 2
        assert _json_block(result) == result.structured_content

    @pytest.mark.asyncio
    async def test_a_request_that_raised_is_named_too(self):
        """A 5xx never produces an `X-Search-Status`, so `search_status` reads
        "ok" while a third of the range is missing. That is the exact result a
        model reads as the whole answer, so it gets a coverage line as well --
        the payload already calls itself partial, so this is not the clean
        result the `ok` path exists to protect.
        """
        result = await call_result(
            _per_date({"2026-09-22": "boom"}),
            "search_oneway_flights",
            **RANGE_ARGS,
        )
        line = _first_line(result)
        assert line.startswith("COVERAGE WARNING: 2 of 3 searches completed.")
        assert "2026-09-22 to BUD" in line
        assert not result.is_error

    def test_a_long_missing_list_is_counted_rather_than_recited(self):
        from src.status_text import partial_first_line

        missing = [f"2026-09-{day:02d} to BUD" for day in range(1, 13)]
        line = partial_first_line(completed=3, attempted=15, missing=missing)
        assert f"and {12 - MAX_NAMED_COMBINATIONS} more" in line
        assert line.count("2026-09-") == MAX_NAMED_COMBINATIONS

    def test_it_falls_back_to_counts_when_nothing_can_be_named(self):
        from src.status_text import partial_first_line

        line = partial_first_line(completed=9, attempted=12, missing=[])
        assert line.startswith("COVERAGE WARNING: 9 of 12 searches completed.")
        assert "Nothing came back for" not in line


class TestACleanResultIsLeftAlone:
    """A clean search must not be made to look alarming."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["ok", "empty"])
    async def test_no_prose_line_is_added(self, status):
        rows = {"2026-09-20": "ok" if status == "ok" else "empty-clean"}

        def handler(request: httpx.Request) -> httpx.Response:
            body = [] if status == "empty" else [_row("2026-09-20")]
            return httpx.Response(
                200, json=body, headers={"X-Search-Status": status}
            )

        result = await call_result(
            build_with_upstream(handler), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == result.structured_content
        assert result.structured_content["search_status"] == status
        assert "WARNING" not in result.content[0].text

    @pytest.mark.asyncio
    async def test_a_silent_backend_gets_no_line_either(self):
        """No header means we do not know the search failed, so we cannot say so."""
        result = await call_result(
            build_with_upstream(
                lambda _r: httpx.Response(200, json=[ONEWAY_ROW])
            ),
            "search_oneway_flights",
            **ONEWAY_ARGS,
        )
        assert len(result.content) == 1
        assert "search_status" not in result.structured_content


class TestTheSerializerMatchesFastMCPs:
    """Our JSON block must be byte-identical to the one FastMCP builds.

    `serialize_payload` reimplements `fastmcp.tools.base.default_serializer`
    one layer down (`pydantic_core.to_json`, same `fallback=str`) rather than
    importing a private helper. This compares the two on a real result instead
    of trusting that equivalence: the `ok` path's block is FastMCP's own, the
    partial path's is ours.
    """

    @pytest.mark.asyncio
    async def test_our_block_and_fastmcps_agree_on_the_same_payload(self):
        from src.status_text import serialize_payload

        auto = await call_result(
            _per_date({}), "search_oneway_flights", **ONEWAY_ARGS
        )
        assert len(auto.content) == 1
        assert auto.content[0].text == serialize_payload(auto.structured_content)
