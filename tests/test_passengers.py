"""`passengers`: what the client sends versus what the backend gets.

The backend reads the list as one Google code per traveller (1 adult, 2 child,
3 infant on lap, 4 infant in seat) and, since flight_rabbi #511, 422s any list
it cannot search as sent. Until 2026-09-26 this server's own schema said
"counts as [adults, children, infants]", so a model following it sent
[2, 1, 0]; forwarded unchanged that is now a billed 422, reported to the model
as "temporarily unavailable" -- which a paying MCP user hit three times in
three seconds on 2026-09-27 17:28Z (`[validation_reject] ... loc=passengers:
assertion source=paid-mcp tool=search_oneway_flights`).

Reproduced live through flights.flightpowers.com/mcp on 2026-09-28 09:57Z:
[2, 1, 0] -> isError, "Flight search is temporarily unavailable (... HTTP 422:
passengers - [2, 1, 0] is not a valid passenger list; 0 is not a passenger
type ...)"; [1, 2] -> 200 with fares.
"""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from src.passengers import (
    counts_to_codes,
    is_codes_list,
    is_upstream_reject,
    normalise_passengers,
)
from tests.test_server import ONEWAY_ROW, build_with_upstream, call


class TestNormalise:
    """The mapping table. Codes win whenever the list is valid as codes."""

    @pytest.mark.parametrize(
        "sent, forwarded",
        [
            # A valid codes list goes through untouched.
            ([1], [1]),
            ([1, 1], [1, 1]),
            ([1, 2], [1, 2]),          # one adult, one child (as documented)
            ([2, 1], [2, 1]),          # same party, order is the caller's
            ([1, 1, 2], [1, 1, 2]),
            ([1, 3], [1, 3]),
            ([1, 4, 4], [1, 4, 4]),
            ([1] * 9, [1] * 9),
            # A counts list that cannot be codes is expanded.
            ([2, 1, 0], [1, 1, 2]),    # the pre-#511 schema's own shape
            ([1, 0, 0], [1]),          # "one adult" from that schema
            ([2], [1, 1]),             # what a model means by [2]
            ([3], [1, 1, 1]),
            ([2, 2], [1, 1, 2, 2]),    # as codes: two children alone
            ([2, 0, 1], [1, 1, 3]),
            ([2, 0, 0, 1], [1, 1, 4]), # fourth slot: infant in seat
            ([5], [1] * 5),
            ([4, 5], [1] * 4 + [2] * 5),
        ],
    )
    def test_mapping(self, sent, forwarded):
        assert normalise_passengers(sent) == forwarded

    @pytest.mark.parametrize(
        "sent",
        [
            [],                 # empty: the backend's 422 says so
            [0],                # no adult either way
            [0, 1],
            [0, 0, 0],
            [-1],
            [2, -1],
            [1, 3, 3],          # codes, but more lap infants than adults
            [1] * 10,           # ten travellers
            [10],               # ten adults as a count
            [5, 5],             # ten travellers as counts
            [2, 1, 0, 0, 0],    # five entries is neither
            [1, 0, 0, 0, 0, 0],
            [7],                # seven adults is fine...
        ],
    )
    def test_unrecognised_lists_are_forwarded_unchanged(self, sent):
        """The backend stays the authority: its 422 names the field and
        spells out the codes. Except [7], which is a legal count."""
        out = normalise_passengers(sent)
        if sent == [7]:
            assert out == [1] * 7
        else:
            assert out is sent

    def test_none_stays_none(self):
        assert normalise_passengers(None) is None

    @pytest.mark.parametrize("sent", [2, "2", "1,1,2", [True, False], [1.0, 2.0], {"adults": 2}])
    def test_non_int_lists_are_left_for_the_caller(self, sent):
        """FastMCP rejects these before the tool runs (list[int] | None); the
        helper must not turn a bool or a float into a party."""
        assert normalise_passengers(sent) is sent

    def test_codes_list_needs_an_adult(self):
        assert is_codes_list([1, 2])
        assert not is_codes_list([2])
        assert not is_codes_list([2, 2])
        assert not is_codes_list([])
        assert not is_codes_list([1, 5])

    def test_counts_need_an_adult_and_at_most_nine(self):
        assert counts_to_codes([1]) == [1]
        assert counts_to_codes([0, 1]) is None
        assert counts_to_codes([9]) == [1] * 9
        assert counts_to_codes([9, 1]) is None
        assert counts_to_codes([1, 1, 1, 1, 1]) is None


class TestToolsForwardCodes:
    """Through the real tool, what body reaches the upstream."""

    @staticmethod
    def _recording_upstream():
        bodies: list[dict] = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=[ONEWAY_ROW])

        return bodies, build_with_upstream(handler)

    @pytest.mark.asyncio
    async def test_oneway_counts_list_reaches_the_backend_as_codes(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="LHR",
            to_airport="DXB",
            departure_date="2026-12-15",
            passengers=[2, 1, 0],
        )
        assert bodies and bodies[0]["passengers"] == [1, 1, 2]

    @pytest.mark.asyncio
    async def test_oneway_codes_list_is_forwarded_as_sent(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="LHR",
            to_airport="DXB",
            departure_date="2026-12-15",
            passengers=[1, 2],
        )
        assert bodies and bodies[0]["passengers"] == [1, 2]

    @pytest.mark.asyncio
    async def test_oneway_omitted_stays_omitted(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="LHR",
            to_airport="DXB",
            departure_date="2026-12-15",
        )
        assert bodies and "passengers" not in bodies[0]

    @pytest.mark.asyncio
    async def test_roundtrip_counts_list_reaches_the_backend_as_codes(self):
        bodies, mcp = self._recording_upstream()
        await call(
            mcp,
            "search_roundtrip_flights",
            from_airport="LHR",
            to_airport="DXB",
            departure_date="2026-12-15",
            return_date="2026-12-22",
            passengers=[2],
        )
        assert bodies and bodies[0]["passengers"] == [1, 1]

    @pytest.mark.asyncio
    async def test_a_bare_integer_is_refused_before_any_upstream_call(self):
        """Pydantic's own list_type error, naming the field; nothing billed.
        Verified identical live on 2026-09-28: passengers=2 -> '1 validation
        error ... passengers Input should be a valid list'."""
        bodies, mcp = self._recording_upstream()
        with pytest.raises(ToolError, match="passengers"):
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="LHR",
                to_airport="DXB",
                departure_date="2026-12-15",
                passengers=2,
            )
        assert bodies == []


UPSTREAM_422 = {
    "detail": (
        "passengers - [1, 3, 3] is not a valid passenger list; each infant on "
        "lap (3) needs its own adult (1)"
    ),
    "field": "passengers",
}


class TestRefusalIsNotAnOutage:
    """A 422 on every combination is the backend refusing the input."""

    @pytest.mark.asyncio
    async def test_upstream_422_reads_as_a_refusal_with_the_detail(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(422, json=UPSTREAM_422))
        with pytest.raises(ToolError) as exc:
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="LHR",
                to_airport="DXB",
                departure_date="2026-12-15",
                passengers=[1, 3, 3],
            )
        text = str(exc.value)
        assert "refused" in text
        assert "temporarily unavailable" not in text
        assert "each infant on lap (3) needs its own adult" in text

    @pytest.mark.asyncio
    async def test_upstream_500_is_still_an_outage(self):
        mcp = build_with_upstream(lambda _r: httpx.Response(500, text="boom"))
        with pytest.raises(ToolError, match="temporarily unavailable"):
            await call(
                mcp,
                "search_oneway_flights",
                from_airport="LHR",
                to_airport="DXB",
                departure_date="2026-12-15",
            )

    def test_reject_marker(self):
        assert is_upstream_reject("oneway search failed -- HTTP 422: passengers - x")
        assert is_upstream_reject("oneway search failed -- HTTP 400: bad json")
        assert not is_upstream_reject("oneway search failed -- HTTP 503: degraded")
        assert not is_upstream_reject("ReadTimeout: timed out")
        assert not is_upstream_reject(None)
        assert not is_upstream_reject("")


class TestSchemaTextStaysHonest:
    @pytest.mark.parametrize(
        "name", ["search_oneway_flights", "search_roundtrip_flights"]
    )
    def test_description_names_both_shapes(self, name):
        from tests.test_directory_readiness import tools_for

        tool = next(t for t in tools_for("flights") if t["name"] == name)
        text = tool["inputSchema"]["properties"]["passengers"]["description"]
        assert "One entry per traveller" in text
        assert "[2, 1, 0]" in text
        assert "converted to codes" in text
        assert "counts as" not in text
