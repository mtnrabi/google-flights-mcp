"""A degraded 5xx is sent once and billed once.

`X-Search-Status: degraded` on a 5xx means the backend ran the search and it
did not complete; its body says whether the same request is worth sending
again (`retry`). The client used to resend every 5xx once, so a `retry: false`
search (an adult long stay with `use_fallback: false`) was billed twice on the
caller's RapidAPI key, and the tool said "temporarily unavailable". Now: one
request, and a tool error that leads with the backend's verdict and hint.
"""

import httpx
import pytest
from fastmcp.exceptions import ToolError

from src.output_schema import SEARCH_STATUS_DESCRIPTION
from tests.test_server import build_with_upstream, call

BACKEND_MESSAGE = (
    "The flight search did not complete because Google showed no flight list "
    "for this search. Retrying the same request is not expected to help."
)
HINT = (
    "This long-stay round trip could not be priced as one search; price the "
    "two one-way legs instead."
)

ROUNDTRIP = {
    "from_airport": "GRU",
    "to_airport": "NRT",
    "departure_date": "2027-03-01",
    "return_date": "2027-04-15",
}


def _degraded_backend(retry, counter):
    body = {
        "error": "search_incomplete",
        "search_status": "degraded",
        "search_reason": "upstream_no_render",
        "message": BACKEND_MESSAGE,
        "hint": HINT,
    }
    if retry is not None:
        body["retry"] = retry

    def handler(_request):
        counter["n"] += 1
        return httpx.Response(
            503,
            json=body,
            headers={
                "X-Search-Status": "degraded",
                "X-Search-Reason": "upstream_no_render",
            },
        )

    return handler


@pytest.mark.asyncio
async def test_retry_false_is_sent_once_and_says_do_not_repeat():
    counter = {"n": 0}
    mcp = build_with_upstream(_degraded_backend(False, counter))
    with pytest.raises(ToolError) as exc:
        await call(mcp, "search_roundtrip_flights", **ROUNDTRIP)

    assert counter["n"] == 1
    text = str(exc.value)
    assert "will not help (retry: false)" in text
    assert HINT in text
    assert BACKEND_MESSAGE in text
    assert "temporarily unavailable" not in text


@pytest.mark.asyncio
async def test_retry_true_is_sent_once_and_leaves_the_resend_to_the_caller():
    counter = {"n": 0}
    mcp = build_with_upstream(_degraded_backend(True, counter))
    with pytest.raises(ToolError) as exc:
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="JFK",
            to_airport="ACC",
            departure_date="2027-06-20",
        )

    assert counter["n"] == 1
    text = str(exc.value)
    assert "(retry: true)" in text
    assert "billed request" in text


@pytest.mark.asyncio
async def test_no_verdict_still_names_no_resend():
    counter = {"n": 0}
    mcp = build_with_upstream(_degraded_backend(None, counter))
    with pytest.raises(ToolError) as exc:
        await call(mcp, "search_roundtrip_flights", **ROUNDTRIP)

    assert counter["n"] == 1
    assert "was not resent" in str(exc.value)


@pytest.mark.asyncio
async def test_a_plain_503_is_still_resent_and_still_an_outage():
    counter = {"n": 0}

    def handler(_request):
        counter["n"] += 1
        return httpx.Response(503, text="upstream down")

    mcp = build_with_upstream(handler)
    with pytest.raises(ToolError, match="temporarily unavailable"):
        await call(mcp, "search_roundtrip_flights", **ROUNDTRIP)
    assert counter["n"] == 2


def test_the_schema_no_longer_calls_a_degraded_result_safe_to_retry():
    assert "safe to retry" not in SEARCH_STATUS_DESCRIPTION
    assert "retry: false" in SEARCH_STATUS_DESCRIPTION


# ── the wording, unit-level ─────────────────────────────────────────────


def _err(retry, combo=None, hint=None):
    from src.rapidapi_client import DegradedSearchError

    exc = DegradedSearchError(
        "roundtrip search did not complete -- HTTP 503", retry, None, hint
    )
    exc.combo = combo
    return exc


def test_a_trial_call_is_not_told_about_a_bill_on_its_plan():
    """A keyless trial runs on OUR key: there is no plan of theirs to bill."""
    from src.server import _degraded_tool_error

    for retry in (True, None):
        text = _degraded_tool_error([_err(retry)], trial=True)
        assert "billed" not in text
        assert "your plan" not in text
        assert "not resent automatically" in text
        keyed = _degraded_tool_error([_err(retry)])
        assert "billed request on your plan" in keyed


def test_mixed_verdicts_name_only_the_combinations_that_may_get_through():
    from src.server import _degraded_tool_error

    errors = [
        _err(True, {"departure_date": "2027-06-20", "to_airport": "ACC"}),
        _err(False, {"departure_date": "2027-06-21", "to_airport": "ACC"}),
        _err(None, {"departure_date": "2027-06-22", "to_airport": "ACC"}),
    ]
    text = _degraded_tool_error(errors)
    assert "only 2027-06-20 to ACC may get through" in text
    assert "do not repeat the whole search unchanged" in text
    assert "the same search may get through" not in text
    assert "2027-06-21" not in text.split("(")[0]


def test_mixed_verdicts_without_combos_fall_back_to_counts():
    from src.server import _degraded_tool_error

    text = _degraded_tool_error([_err(True), _err(False), _err(False)])
    assert "only 1 of the 3 searches may get through" in text
    assert "the same search may get through" not in text


@pytest.mark.asyncio
async def test_mixed_verdicts_end_to_end_name_the_date():
    """Through the real client and fan-out: one date says retry true, the
    other two retry false; the error names the one date, each sent once."""
    import json

    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        sent = json.loads(request.content)
        retry = sent.get("departure_date") == "2027-06-21"
        return httpx.Response(
            503,
            json={"search_status": "degraded", "retry": retry},
            headers={"X-Search-Status": "degraded"},
        )

    mcp = build_with_upstream(handler)
    with pytest.raises(ToolError) as exc:
        await call(
            mcp,
            "search_oneway_flights",
            from_airport="JFK",
            to_airport="ACC",
            departure_date_from="2027-06-20",
            departure_date_to="2027-06-22",
        )

    assert counter["n"] == 3
    assert "only 2027-06-21 to ACC may get through" in str(exc.value)
