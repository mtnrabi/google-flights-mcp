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

#: The `reason` vocabulary on a `by_destination` entry. `_by_destination` in
#: src/server.py is what produces the values; the comment above it says what
#: each one means.
DESTINATION_REASONS = (
    "ok",
    "no_flights",
    "search_failed",
    "not_in_limit",
    "not_searched",
)

_BY_DESTINATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One entry per destination the REQUEST asked for, in request order, "
        "present whether or not that destination has any flights in "
        "`results`. A destination with an empty `rows` array is a hole in the "
        "answer, and `reason` says which kind of hole: 'no_flights' (searched, "
        "answered, Google has nothing), 'search_failed' (searched and the "
        "search errored, so nothing is known), 'not_in_limit' (searched, found "
        "flights, none fitted in `limit`) or 'not_searched' (never searched -- "
        "the per-call fan-out cap sampled it away). 'ok' means it has rows.\n\n"
        "Read this rather than inferring coverage from `results`: a "
        "destination missing from `results` looks identical to one that has "
        "no flights, and they are not the same answer. `rows` are the same "
        "row objects that are in `results`, in the same order -- nothing here "
        "is data the answer does not already contain."
    ),
    "additionalProperties": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            # `anyOf` with one type per branch, not `"type": ["object",
            # "null"]`. Both are valid JSON Schema and mean the same thing,
            # but a type ARRAY is the form tools in the wild handle worst:
            # the MCP Inspector flags it, and several client-side validators
            # and code generators read only the first entry -- which would
            # make a legitimate `null` here look like a schema violation to
            # the caller. The wire format does not change at all.
            "cheapest": {
                "description": (
                    "The lowest-priced of this destination's rows in "
                    "`results`, or null when it has none."
                ),
                "anyOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ],
            },
            "searched": {
                "type": "boolean",
                "description": (
                    "Whether at least one search actually ran for this "
                    "destination. False means the fan-out cap dropped it."
                ),
            },
            "reason": {"type": "string", "enum": list(DESTINATION_REASONS)},
            "dates": {
                "type": "object",
                "description": (
                    "Present only on multi-date searches: one entry per "
                    "departure date requested for this destination, so a date "
                    "the fan-out cap sampled away is visible rather than "
                    "absent. `cheapest_price` is null when that date has no "
                    "row in `results`."
                ),
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "searched": {"type": "boolean"},
                        "reason": {
                            "type": "string",
                            "enum": list(DESTINATION_REASONS),
                        },
                        "row_count": {"type": "integer", "minimum": 0},
                        "cheapest_price": {
                            "anyOf": [{"type": "number"}, {"type": "null"}]
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
        "required": ["rows", "searched", "reason"],
        "additionalProperties": True,
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
        "by_destination": _BY_DESTINATION,
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

#: The vocabulary a per-source row uses. `skipped` is not a search outcome at
#: all -- it means the source was never called -- and it is deliberately in the
#: same enum, because a client reading these rows has to distinguish "asked and
#: failed" from "never asked" and both arrive in the same list.
PROVIDER_STATUS_VALUES = ("ok", "empty", "degraded", "skipped")

_PROVIDER_ROW: dict[str, Any] = {
    "type": "object",
    "description": (
        "What one accommodation source answered. Present for every source "
        "that was CALLED, whether or not it answered: a source with "
        "search_status 'degraded' carries a reason, a null count and no "
        "prices, which is not the same answer as a source that found nothing."
    ),
    "properties": {
        "provider": {
            "type": "string",
            "description": "Which source this row is about, e.g. 'booking'.",
        },
        "search_status": {
            "type": "string",
            "enum": list(PROVIDER_STATUS_VALUES),
            "description": (
                "'ok': the source answered with priced results. 'empty': it "
                "answered and had nothing for that stay -- a real answer. "
                "'degraded': it was asked and could not answer, so count is "
                "null and an empty list from it means nothing."
            ),
        },
        "search_reason": {"type": "string"},
        "detail": {
            "type": "string",
            "description": "Plain text for the model to relay to a human.",
        },
        "count": {
            "description": (
                "How many properties from this source carried a price. Null "
                "when the source did not answer -- zero would read as 'nothing "
                "there', which a failed search does not know."
            ),
            "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
        },
        "cheapest_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "cheapest_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "cheapest_link": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "median_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "currency": {
            "description": (
                "The currency THIS row's totals are in. Rows in different "
                "currencies are not comparable and nothing is converted."
            ),
            "anyOf": [{"type": "string"}, {"type": "null"}],
        },
        "rating_scale": {
            "description": (
                "What a review score from this source is out of: 10 on "
                "Booking, 5 on Airbnb. Read it before comparing two scores."
            ),
            "anyOf": [{"type": "integer"}, {"type": "null"}],
        },
        "taxes_included": {
            "description": (
                "Whether this source's totals include tax. Null means it has "
                "not been established for that source, not that tax is "
                "excluded."
            ),
            "anyOf": [{"type": "boolean"}, {"type": "null"}],
        },
        "retrieved_at": {
            "description": (
                "When these rows were read, taken from the rows themselves "
                "rather than from when this answer was assembled."
            ),
            "anyOf": [{"type": "string"}, {"type": "null"}],
        },
        "top": {
            "type": "array",
            "description": "The cheapest few priced rows from this source.",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["provider", "search_status"],
    "additionalProperties": True,
}

_PROVIDERS_SKIPPED: dict[str, Any] = {
    "type": "array",
    "description": (
        "Sources that were NOT called, each named with why and where to "
        "subscribe. A source here contributed nothing to results and is "
        "counted nowhere. It is listed rather than dropped because a silently "
        "missing source is indistinguishable from a source that had nothing."
    ),
    "items": {
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "reason": {
                "type": "string",
                "description": (
                    "'no_key': no RapidAPI key for that source's listing "
                    "arrived with the call. 'not_subscribed': the key supplied "
                    "is not subscribed to that listing. 'key_rejected': the "
                    "key was refused outright."
                ),
            },
            "detail": {"type": "string"},
            "subscribe_url": {"type": "string"},
        },
        "required": ["provider", "reason"],
        "additionalProperties": True,
    },
}

_CAVEATS: dict[str, Any] = {
    "type": "array",
    "description": (
        "Sentences that must be read before one source is called cheaper than "
        "another -- differing rating scales, differing tax treatment, "
        "differing currencies, a source that did not answer."
    ),
    "items": {"type": "string"},
}


#: The `reason` vocabulary on a `stays` entry, mirroring STAY_REASONS in
#: src/server.py. Add a value in both places or a validating client rejects
#: the response.
STAY_REASONS = (
    "ok",
    "no_availability",
    "no_price",
    "search_failed",
    "not_searched",
)

_STAY_PROPERTY: dict[str, Any] = {
    "description": (
        "The cheapest priced property for this stay, or null when it has "
        "none. The same row the upstream returned, minus its image URL: on a "
        "measured search 73% of the payload was URLs, and an image CDN link "
        "is the half no model can open. The booking link is kept."
    ),
    "anyOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "null"},
    ],
}

_STAYS: dict[str, Any] = {
    "type": "array",
    "description": (
        "One entry per stay the REQUEST asked for, in request order, present "
        "whether or not that stay was priced. Only on a date-range search "
        "(checkin_date_from / checkin_date_to / nights); a single stay does "
        "not carry it.\n\n"
        "Read this rather than inferring coverage from `results`: `results` "
        "holds the full property list for the CHEAPEST stay only, so a stay "
        "missing from it looks identical to a stay that had nothing. `reason` "
        "says which kind of hole an unpriced stay is: 'no_availability' "
        "(searched, answered, nothing came back), 'no_price' (properties came "
        "back, none carried a price), 'search_failed' (the search errored, so "
        "nothing is known -- not 'no rooms') or 'not_searched' (the per-call "
        "fan-out cap sampled it away). Counts are null rather than zero on "
        "those last two, because zero reads as 'nothing there' and neither "
        "case knows that."
    ),
    "items": {
        "type": "object",
        "properties": {
            "checkin_date": {"type": "string"},
            "checkout_date": {"type": "string"},
            "nights": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "search_status": {
                "type": "string",
                "enum": ["ok", "empty", "degraded", "not_searched"],
            },
            "reason": {"type": "string", "enum": list(STAY_REASONS)},
            "property_count": {
                "description": (
                    "Properties returned for this stay. Null when it was not "
                    "searched or its search errored."
                ),
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
            },
            "priced_count": {
                "description": (
                    "How many of those carried a price. The others are not "
                    "evidence about what the stay costs."
                ),
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
            },
            "cheapest_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "price_per_night": {
                "description": "cheapest_total divided by nights, rounded.",
                "anyOf": [{"type": "number"}, {"type": "null"}],
            },
            "median_total": {
                "description": (
                    "Median stay total over this stay's priced properties -- "
                    "what the date costs generally, next to what its one "
                    "cheapest room costs."
                ),
                "anyOf": [{"type": "number"}, {"type": "null"}],
            },
            "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "cheapest": _STAY_PROPERTY,
        },
        "required": ["checkin_date", "checkout_date", "search_status", "reason"],
        "additionalProperties": True,
    },
}

_CHEAPEST_OVERALL: dict[str, Any] = {
    "description": (
        "The cheapest stay across everything priced, or null when nothing "
        "was. `results` holds this stay's full property list."
    ),
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "checkin_date": {"type": "string"},
                "checkout_date": {"type": "string"},
                "nights": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "total": {"type": "number"},
                "price_per_night": {
                    "anyOf": [{"type": "number"}, {"type": "null"}]
                },
                "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "property": _STAY_PROPERTY,
            },
            "additionalProperties": True,
        },
        {"type": "null"},
    ],
}

_STAY_COVERAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "What was actually priced on a date-range search. Present whether or "
        "not the request was truncated, so a model can state honestly what "
        "its answer rests on."
    ),
    "properties": {
        "requested_combinations": {"type": "integer", "minimum": 0},
        "searched_combinations": {"type": "integer", "minimum": 0},
        "truncated": {
            "type": "boolean",
            "description": (
                "True when the request expanded past this call's spend "
                "ceiling and was sampled. A stay absent from stays_searched "
                "was never priced, which is not the same as having no rooms."
            ),
        },
        "max_searches_per_request": {"type": "integer", "minimum": 1},
        "stays_searched": {
            "type": "array",
            "description": "The exact date pairs priced.",
            "items": {
                "type": "object",
                "properties": {
                    "checkin_date": {"type": "string"},
                    "checkout_date": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "checkin_dates_searched": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "additionalProperties": True,
}


HOTELS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Hotel search result",
    "description": (
        "A completed hotel search. A single stay carries no search_status: "
        "the hotels upstream reports no search-status header, and one call "
        "either answered or raised. A DATE-RANGE search does carry one, "
        "derived from the fan-out rather than from the upstream -- some of "
        "its stays can fail while others answer, and `results` then holds "
        "only the cheapest stay's properties, so `stays` is where the shape "
        "of the answer lives."
    ),
    "properties": {
        "results": _RESULT_ROWS,
        "result_count": {"type": "integer", "minimum": 0},
        "stays": _STAYS,
        "results_for_stay": {
            "description": (
                "Which stay `results` belongs to on a date-range search, or "
                "null when nothing was priced. Without it the rows read as "
                "'the search's results' and get quoted against the wrong "
                "dates."
            ),
            "anyOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "null"},
            ],
        },
        "cheapest_overall": _CHEAPEST_OVERALL,
        "search_status": {
            "type": "string",
            "enum": ["ok", "empty", "partial", "degraded"],
            "description": (
                "Date-range searches only. 'ok': every stay searched was "
                "priced. 'empty': they all answered and none had priced "
                "availability -- a real answer. 'partial': some stays were "
                "priced and some errored. 'degraded': every stay errored, so "
                "nothing is known; safe to retry."
            ),
        },
        "search_coverage": _STAY_COVERAGE,
        "partial": {
            "type": "string",
            "description": (
                "Present when some stays failed but others were priced. Plain "
                "text saying how much of the request the answer covers."
            ),
        },
        "api_usage": _API_USAGE,
        "applied_filters": {
            "description": (
                "Which of the requested filters the upstream actually applied. "
                "Untyped: the shape is the upstream's, echoed through."
            )
        },
        "message": {"type": "string"},
        # Present only when more than the default source was asked for. A
        # single-source search is unchanged, field for field, which is what
        # makes `providers` safe to add to a tool that already has callers.
        "providers": {
            "type": "array",
            "description": (
                "One entry per source that was called, in the order they were "
                "requested. Only present when `providers` named more than the "
                "default source."
            ),
            "items": _PROVIDER_ROW,
        },
        "providers_skipped": _PROVIDERS_SKIPPED,
        "caveats": _CAVEATS,
        **_ACCOUNT_PROPERTIES,
    },
    "required": ["results"],
    "additionalProperties": True,
}

COMPARE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Cross-source hotel rate comparison",
    "description": (
        "One stay priced on every source the caller has a key for, one row "
        "per source. Read `caveats` and each row's `search_status`, "
        "`rating_scale` and `currency` before saying one source is cheaper "
        "than another: a degraded source has no count and no prices, and a "
        "skipped source is counted nowhere."
    ),
    "properties": {
        "destination": {"type": "string"},
        "checkin_date": {"type": "string"},
        "checkout_date": {"type": "string"},
        "nights": {
            "description": (
                "Nights between the two dates, or null when the dates could "
                "not be read as dates."
            ),
            "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
        },
        "adults": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "children": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "currency": {
            "description": "The currency every source was ASKED for.",
            "anyOf": [{"type": "string"}, {"type": "null"}],
        },
        "providers": {"type": "array", "items": _PROVIDER_ROW},
        "providers_skipped": _PROVIDERS_SKIPPED,
        "caveats": _CAVEATS,
        "api_usage": _API_USAGE,
        "message": {"type": "string"},
        **_ACCOUNT_PROPERTIES,
    },
    # The one key on every exit path, including the keyless reply: same
    # argument as `results` on the two schemas above.
    "required": ["providers"],
    "additionalProperties": True,
}
