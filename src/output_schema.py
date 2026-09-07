"""Declared MCP output schemas for this server's tools.

Why this file exists
--------------------
These tools have always answered with a JSON object and have always carried a
`search_status` field. What they never did was *declare* any of it. FastMCP
derives an output schema from the `-> dict[str, Any]` return annotation, and
for a bare dict that derivation is::

    {"type": "object", "additionalProperties": true}

which is a schema in name only: it tells a client that the result is an
object and nothing else. So `search_status` travelled as an undeclared
convention -- a key a caller could only learn about by reading our prose and
hoping we did not rename it.

The MCP specification already has the mechanism for this. `outputSchema` on
the tool plus `structuredContent` on the result were added in revision
2025-06-18 ("Add support for structured tool output") and are unchanged in
substance in the current revision, 2026-07-28, which loosened `outputSchema`
to any JSON Schema 2020-12 and `structuredContent` to any JSON value. The
spec's rule for us is a MUST:

    "Servers MUST provide structured results that conform to this schema.
     Clients SHOULD validate structured results against this schema."

That MUST is the whole reason for the shape below. Every schema here is
`additionalProperties: true` with `required` limited to the one key that
genuinely appears on every exit path. It is tempting to write a tight schema
listing exactly the keys of a successful search, and it would be wrong: these
tools have several legitimate non-search exits -- `needs_api_key` when the
caller sent no RapidAPI key or one the upstream rejected, and
`quota_exhausted` when their plan is spent. Both are returned as data rather
than raised as errors on purpose, because the model has to relay their
instructions to a human and a structured result survives that trip better
than an exception string. A schema that forbade them would make the server
violate the MUST on a path we ship deliberately.

Note that FastMCP builds every tool on both deployments before pruning the
ones the product does not serve, so a malformed schema here breaks the cold
start of the flights deployment and the hotels deployment alike.

Backwards compatibility
-----------------------
Nothing here stops the serialized JSON text block being sent. The spec asks
for it --

    "For backwards compatibility, a tool that returns structured content
     SHOULD also return the serialized JSON in a TextContent block."

-- and we have callers on clients that predate structured content entirely.
FastMCP emits both from a single returned dict, and the one place we build a
result by hand (`ToolResult` for a degraded search) passes only
`structured_content`, which makes FastMCP derive the identical text block
through the identical serializer.
"""

from typing import Any

# The four values `search_status` can take, mirroring the backend's own
# `X-Search-Status` vocabulary (backend/src/flight_search/search_outcome.py).
#
# `degraded` is in this list even though a degraded search is now returned
# with `isError: true`. Dropping it would have been the tidier-looking
# choice and a worse one -- a degraded result still carries its coverage and
# its api_usage, and a client validating that payload against this schema
# must find the status value it actually contains. See the comment on
# `is_degraded` in server.py for why the error flag is the part that matters.
SEARCH_STATUS_VALUES = ("ok", "empty", "partial", "degraded")

SEARCH_STATUS_DESCRIPTION = (
    "Whether the underlying search actually completed, read from the "
    "backend's X-Search-Status header. 'ok': every combination searched "
    "returned results. 'empty': the search completed and Google genuinely "
    "has no itineraries for it -- a real answer, not a failure. 'partial': "
    "some combinations returned results and some failed, so the list is "
    "incomplete. 'degraded': every combination failed, so the search did not "
    "happen and an empty list means nothing; this case is also flagged with "
    "isError: true and is safe to retry."
)

_RESULT_ROWS: dict[str, Any] = {
    "type": "array",
    "description": (
        "The itineraries or properties found, already sorted and deduplicated. "
        "An empty array is only meaningful when search_status is 'empty'."
    ),
    "items": {"type": "object", "additionalProperties": True},
}

# Every response that reached the upstream carries this, because the money is
# the caller's. Declared field by field so a client can bill against it
# rather than parse the sentence in `note`.
_API_USAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "What this call cost the caller's own RapidAPI plan, and what remains "
        "on it. Present on every response that reached the upstream, including "
        "a degraded one -- a search that failed was still billed."
    ),
    "properties": {
        "requests_used_by_this_call": {"type": "integer", "minimum": 0},
        "plan_requests_remaining": {"type": "integer"},
        "plan_requests_limit": {"type": "integer"},
        "note": {
            "type": "string",
            "description": "The same figures as a sentence, for the model to relay.",
        },
    },
    "additionalProperties": True,
}

_FLIGHT_COVERAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "What was actually searched. Present whether or not the request was "
        "truncated, so a model can state honestly what its answer rests on."
    ),
    "properties": {
        "requested_combinations": {"type": "integer", "minimum": 0},
        "searched_combinations": {"type": "integer", "minimum": 0},
        "truncated": {
            "type": "boolean",
            "description": (
                "True when the request expanded past this call's spend ceiling "
                "and was sampled. A date absent from departure_dates_searched "
                "was never searched, which is not the same as having no flights."
            ),
        },
        "max_searches_per_request": {"type": "integer", "minimum": 1},
        "departure_dates_searched": {"type": "array", "items": {"type": "string"}},
        "destinations_searched": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "additionalProperties": True,
}

_ACCOUNT_PROPERTIES: dict[str, Any] = {
    "needs_api_key": {
        "type": "boolean",
        "description": (
            "True when no usable RapidAPI key arrived with the call, or the "
            "upstream rejected the one that did. No search was run and nothing "
            "was billed; signup_url and message say how to fix it."
        ),
    },
    "quota_exhausted": {
        "type": "boolean",
        "description": (
            "True when the caller's RapidAPI plan has no requests left for the "
            "current period."
        ),
    },
    "signup_url": {
        "type": "string",
        "description": "Where the caller subscribes or changes plan.",
    },
}

FLIGHTS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Flight search result",
    "description": (
        "A completed flight search. Read search_status before reading results: "
        "an empty array means 'no flights' only when search_status is 'empty'."
    ),
    "properties": {
        "results": _RESULT_ROWS,
        "result_count": {"type": "integer", "minimum": 0},
        "search_status": {
            "type": "string",
            "enum": list(SEARCH_STATUS_VALUES),
            "description": SEARCH_STATUS_DESCRIPTION,
        },
        "search_coverage": _FLIGHT_COVERAGE,
        "api_usage": _API_USAGE,
        "partial": {
            "type": "string",
            "description": (
                "Present when some searches failed but others succeeded. Plain "
                "text saying how much of the request the results cover."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "Present when there is something the model must relay to the "
                "user rather than silently absorb -- no results, a degraded "
                "search, a missing key or a spent quota."
            ),
        },
        **_ACCOUNT_PROPERTIES,
    },
    # `results` is the only key on every exit path -- the search result, the
    # zero-result answer, `needs_api_key` and `quota_exhausted` all carry it.
    # Requiring anything else here would make one of those paths violate the
    # spec's "servers MUST provide structured results that conform".
    "required": ["results"],
    "additionalProperties": True,
}

HOTELS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Hotel search result",
    "description": (
        "A completed hotel search. The hotels upstream reports no search-status "
        "header, so these results carry no search_status field."
    ),
    "properties": {
        "results": _RESULT_ROWS,
        "result_count": {"type": "integer", "minimum": 0},
        "api_usage": _API_USAGE,
        "applied_filters": {
            "description": (
                "Which of the requested filters the upstream actually applied. "
                "Untyped: the shape is the upstream's, echoed through."
            )
        },
        "message": {"type": "string"},
        **_ACCOUNT_PROPERTIES,
    },
    "required": ["results"],
    "additionalProperties": True,
}
