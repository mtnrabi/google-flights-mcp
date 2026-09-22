"""Compact flight rows, so a wide search still fits in a host's context.

Why this file exists
--------------------
Found on claude.ai, 2026-09-22: a 279-combination `search_roundtrip_flights`
result (three destinations over a whole October, three trip lengths) is too
big for the host to inject into the conversation. claude.ai answered with

    Tool result too large for context, stored at
    /mnt/user-data/tool_results/mcp__FlightPowers_Flights__search_roundtrip_flights_….json

and handed the model a FILE instead of a tool result. The model still read the
file and its text answer was correct, so nothing looked broken -- but the
MCP-UI card is fed from the injected result, and with no injected result it
sat on "Loading fares…" for ever. The widget was fine; the payload was too fat
to arrive.

Measured on this server the same day (real keyed calls through
`google-flights-mcp.flightpowers.com/mcp`, round trip TLV→FCO[,ATH],
1–31 October, nights 3/4/5):

    combinations   structuredContent   text mirror        total
    93               401,684 B           401,684 B        803,368 B
    186              803,061 B           803,061 B      1,606,122 B
    279 (extrap.)  ~1,204,000 B        ~1,204,000 B     ~2,409,000 B

Three things made it that big and none of them is information the model or
the card uses:

1. **Every row is sent twice.** FastMCP puts `structuredContent` on the
   result and mirrors the same object, serialized, into a TextContent block
   for pre-2025-06-18 clients. At 279 rows that mirror is a megabyte.
2. **`by_destination.rows` was a third and fourth copy.** It held the same
   row objects as `results`, split by destination -- and, on a `nights`
   fan-out, held each of them THREE times, because the per-destination key is
   `(destination, departure_date)` and three trip lengths share one key, so
   the list was walked once per duplicate key. 93 selected rows came back as
   279 in `by_destination` (measured; see `test_compact.py`).
3. **Half of every row is prose the model never reads.** `from_airport`
   repeats the origin on all 279 rows; `departure_flight_arrival_description`
   and its return twin repeat an arrival time the card does not draw;
   `*_stops_info` is almost always `[]`; `*_duration_seconds` duplicates the
   human duration string next to it.

So: keep the cheapest `RESULT_ROWS_MAX` rows, keep on each row only the
fields a model or the card actually reads, hoist the per-row constants to the
response, and stop mirroring a large payload as text. Nothing is lost that a
caller cannot ask for: `verbose: true` returns the old row shape with no cap,
and `RESULT_ROWS_MAX=0` turns the cap off for the whole deployment.

What a compact row keeps
------------------------
The field NAMES are unchanged. That is deliberate: `src/widget.py`, every
example in the listing docs, and every script a customer has written read
`total_price`, `buy_link`, `to_airport` and the rest by name, and renaming
them to save a few bytes would break all of them to no purpose. What changes
is which fields are present, not what they are called.

One-way rows keep the date, destination, airline, stops, duration, price
(string and number), Google's price band, the departure description and the
booking link. Round-trip rows keep the same plus both legs' airline, duration
and departure description, the return date and -- new, and the one field that
is added rather than removed -- `nights`, the trip length, which a `nights`
fan-out makes the whole point of the search and which was previously only
derivable by subtracting two dates.
"""

from __future__ import annotations

from datetime import date
from typing import Any

#: The fare, under both of the names the upstream uses for it: `price` on a
#: one-way row, `total_price` on a round trip. Both are in both field lists
#: on purpose. A fare is the one thing a row exists to carry, dropping one
#: because the row "should" have used the other name is the kind of guess
#: that silently empties a column, and a name the upstream did not send
#: costs nothing because absent fields are skipped.
PRICE_FIELDS: tuple[str, ...] = (
    "price",
    "price_as_number",
    "total_price",
    "total_price_as_number",
)

#: Fields kept on a one-way row, in the order the backend emits them so a
#: compacted row still reads like the row it came from.
ONEWAY_FIELDS: tuple[str, ...] = (
    "price_range_in_relation_to_other_periods",
    "price_insights_low",
    "price_insights_high",
    "to_airport",
    "departure_date",
    *PRICE_FIELDS,
    "duration",
    "airline",
    "stops",
    "departure_description",
    "buy_link",
)

#: Fields kept on a round-trip row. `nights` is computed, not copied.
ROUNDTRIP_FIELDS: tuple[str, ...] = (
    "price_range_in_relation_to_other_periods",
    "price_insights_low",
    "price_insights_high",
    "to_airport",
    "departure_date",
    "return_date",
    "nights",
    *PRICE_FIELDS,
    "total_stops",
    "departure_flight_departure_description",
    "departure_flight_airline",
    "departure_flight_duration",
    "return_flight_departure_description",
    "return_flight_airline",
    "return_flight_duration",
    "buy_link",
)

#: Row fields hoisted out of every row onto the response itself. They are
#: constant across a search by construction -- one origin per search is
#: enforced in `normalise_origin` -- so N copies carry one fact.
HOISTED_FIELDS: tuple[str, ...] = ("from_airport",)


def _is_roundtrip(row: dict[str, Any]) -> bool:
    """A round-trip row, by the fields only a round trip has.

    `total_price` is the backend's own tell (a one-way row carries `price`),
    and the two leg descriptions are the fallback for a row that lost it.
    """
    return any(
        row.get(name) is not None
        for name in (
            "total_price",
            "total_price_as_number",
            "return_date",
            "departure_flight_departure_description",
            "return_flight_departure_description",
        )
    )


def nights_between(departure: Any, ret: Any) -> int | None:
    """Trip length in nights, or None if either date is unreadable.

    Upstream dates are ISO strings. Anything else -- a missing date, a
    malformed one, a return before the departure -- returns None rather than
    a guess, because a wrong trip length is worse than an absent one.
    """
    if not isinstance(departure, str) or not isinstance(ret, str):
        return None
    try:
        out = date.fromisoformat(departure.strip())
        back = date.fromisoformat(ret.strip())
    except ValueError:
        return None
    delta = (back - out).days
    return delta if delta >= 0 else None


def compact_row(row: dict[str, Any], roundtrip: bool | None = None) -> dict[str, Any]:
    """One row, reduced to the fields a model or the card reads.

    `roundtrip` is the TOOL's own answer -- `search_roundtrip_flights` knows
    what it asked for and never has to guess. It is optional only so the
    helper stays usable on a loose row; passing None falls back to reading
    the row's own fields, which is a heuristic and is why the caller in
    src/server.py does not use it.

    A field that is absent upstream stays absent -- this never invents a key
    -- and a field whose value is None is dropped, because `"stops_info":
    null` costs bytes and says nothing.
    """
    if not isinstance(row, dict):
        return row
    if roundtrip is None:
        roundtrip = _is_roundtrip(row)
    fields = ROUNDTRIP_FIELDS if roundtrip else ONEWAY_FIELDS
    source = dict(row)
    if roundtrip and "nights" not in source:
        computed = nights_between(source.get("departure_date"), source.get("return_date"))
        if computed is not None:
            source["nights"] = computed
    return {name: source[name] for name in fields if source.get(name) is not None}


def compact_rows(
    rows: list[dict[str, Any]], roundtrip: bool | None = None
) -> list[dict[str, Any]]:
    return [compact_row(row, roundtrip) for row in rows]


def destination_of(row: Any) -> str:
    """The bucket a row belongs to, the same way the card buckets it.

    `src/widget.py` groups rows on the RAW `to_airport` string and gives each
    bucket a filter chip, so anything that decides which rows survive has to
    bucket them identically or the card loses a chip for a destination the
    caller asked about and paid for. A row with no destination is its own
    bucket -- the card's "Other" chip -- rather than being lumped in with the
    first one.
    """
    if not isinstance(row, dict):
        return ""
    return str(row.get("to_airport") or "")


def bound_rows(rows: list[dict[str, Any]], rows_max: int) -> list[dict[str, Any]]:
    """At most `rows_max` rows, with every destination still represented.

    A head slice off the price-sorted list is the obvious implementation and
    it is wrong for exactly the search this bound exists for. Three
    destinations over a month, 60 rows out of 279: Rome is cheaper than
    Budapest on nearly every date, so the first 60 rows are ~31 Rome, ~29
    Athens and NO Budapest -- a destination that was searched, billed and
    answered vanishes from `results`, and the card (which builds its filter
    chips from the rows it is handed, src/widget.py `bucketsOf`) draws two
    chips for a three-destination question. The same defect
    `_select_rows_by_combo` was written to fix for `limit`, one level up.

    So: two passes, the shape `_select_rows_by_combo` uses. Pass one hands
    out `ceil(rows_max / destinations)` rows to each destination that has
    any, round-robin so a shortfall is shared rather than falling entirely
    on whichever destination sorts last. Pass two fills whatever is left in
    sort order. Rows arrive already ordered by the caller's `sort_by`, so
    "that destination's first N" is its best N, and the returned list keeps
    the original order -- this only ever removes rows, never reorders them.

    Every destination with rows gets at least one while there are at most
    `rows_max` of them. Past that the bound cannot show them all, which is
    why `by_destination` names every destination's own cheapest fare
    regardless of what fits here.
    """
    if rows_max <= 0 or len(rows) <= rows_max:
        return rows

    buckets: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        buckets.setdefault(destination_of(row), []).append(index)

    per = -(-rows_max // len(buckets))  # ceil
    chosen: set[int] = set()
    for rank in range(per):
        if len(chosen) >= rows_max:
            break
        for indices in buckets.values():
            if len(chosen) >= rows_max:
                break
            if rank < len(indices):
                chosen.add(indices[rank])

    for index in range(len(rows)):
        if len(chosen) >= rows_max:
            break
        chosen.add(index)

    return [rows[index] for index in sorted(chosen)]


def hoisted(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-row constants, read off the first row that carries them.

    Read from the rows rather than from the request arguments on purpose:
    `from_airport` in the answer is the backend's own rendering of the origin
    ("Tel Aviv (TLV)"), not the IATA code the caller sent, and the response
    should not quietly change what that field says.
    """
    out: dict[str, Any] = {}
    for name in HOISTED_FIELDS:
        for row in rows:
            if isinstance(row, dict) and row.get(name) is not None:
                out[name] = row[name]
                break
    return out


#: How many destination lines the text summary carries. A host that shows
#: the model only the text block sees THIS and nothing else, so the line
#: that matters -- the cheapest fare for each destination the caller asked
#: about -- has to be in it for every destination, not just the three that
#: happen to top the list. Ten is where a summary stops being a summary;
#: past it the count and the coverage still tell the truth about what was
#: left out.
SUMMARY_DESTINATIONS = 10


def _fare_line(row: dict[str, Any]) -> str:
    """One row, as a sentence, for the text summary."""
    price = row.get("total_price") or row.get("price") or ""
    where = row.get("to_airport") or ""
    when = str(row.get("departure_date") or "")
    back = str(row.get("return_date") or "")
    if back:
        when = f"{when} to {back}"
        nights = row.get("nights")
        if isinstance(nights, int):
            when += f" ({nights} nights)"
    airline = row.get("airline") or row.get("departure_flight_airline") or ""
    stops = row.get("total_stops")
    if stops is None:
        stops = row.get("stops")
    if stops == 0:
        airline = f"{airline} nonstop".strip()
    bits = [str(price), str(where), when, str(airline)]
    return " ".join(bit for bit in bits if bit)


def _cheapest_per_destination(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per destination: its cheapest fare.

    Read from `by_destination` when it is there, because that block is built
    from every selected row BEFORE the row bound and therefore names a
    destination whose fares did not fit in `results`. Falls back to grouping
    `results` for a payload that has no such block.
    """
    by_destination = payload.get("by_destination")
    out: list[dict[str, Any]] = []
    if isinstance(by_destination, dict):
        for entry in by_destination.values():
            if isinstance(entry, dict) and isinstance(entry.get("cheapest"), dict):
                out.append(entry["cheapest"])
    if out:
        return sorted(out, key=_price_of)

    best: dict[str, dict[str, Any]] = {}
    for row in payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        key = destination_of(row)
        if key not in best or _price_of(row) < _price_of(best[key]):
            best[key] = row
    return sorted(best.values(), key=_price_of)


def _price_of(row: dict[str, Any]) -> float:
    for name in ("total_price_as_number", "price_as_number"):
        value = row.get(name)
        if isinstance(value, (int, float)):
            return float(value)
    return float("inf")


def summary_text(payload: dict[str, Any], *, omitted_bytes: int) -> str:
    """The text block for a result too large to mirror as JSON.

    Written for the host that shows a model the text and nothing else. It
    has to stand on its own: the counts, the cheapest fare PER DESTINATION,
    what was actually searched, and -- explicitly, because a model reading
    only this would otherwise answer from four lines -- that the rows are in
    `structuredContent`.
    """
    coverage = payload.get("search_coverage") or {}
    returned = payload.get("results_returned", len(payload.get("results") or []))
    total = payload.get("results_total", returned)
    searched = coverage.get("searched_combinations")
    requested = coverage.get("requested_combinations")

    lines = [f"{returned} of {total} fares found."]

    cheapest = _cheapest_per_destination(payload)
    if cheapest:
        shown = cheapest[:SUMMARY_DESTINATIONS]
        lines.append("Cheapest per destination:")
        lines.extend(f"- {_fare_line(row)}" for row in shown)
        if len(cheapest) > len(shown):
            lines.append(
                f"- and {len(cheapest) - len(shown)} more destinations in "
                "structuredContent.by_destination."
            )

    covered = []
    if searched is not None:
        covered.append(
            f"{searched} date/destination combinations searched"
            + (f" of {requested} requested" if requested is not None else "")
        )
    dates = coverage.get("departure_dates_searched")
    if isinstance(dates, list) and dates:
        covered.append(
            f"departure dates {dates[0]} to {dates[-1]}"
            if len(dates) > 1
            else f"departure date {dates[0]}"
        )
    destinations = coverage.get("destinations_searched")
    if isinstance(destinations, list) and destinations:
        covered.append("destinations " + ", ".join(str(d) for d in destinations))
    if coverage.get("truncated"):
        covered.append("TRUNCATED: the range was sampled, not covered in full")
    if covered:
        lines.append("Coverage: " + "; ".join(covered) + ".")

    lines.append(
        "Every row is in this result's structuredContent -- results (the "
        "fares), by_destination (each destination's own cheapest, whether or "
        "not it fit) and api_usage (what this cost). They are not repeated "
        f"here: the serialized copy would have added {omitted_bytes:,} bytes "
        "to a result that is already large."
    )
    return "\n".join(lines)


def ensure_counts(
    payload: dict[str, Any], *, roundtrip: bool, compact: bool
) -> dict[str, Any]:
    """`results_total` / `results_returned` on a response that has neither.

    EVERY exit path gets them, not only a completed search: a keyless reply,
    a plan that is out of requests, and the quota gate's refusal all answer
    with a `results` array (the gate's can even carry the fares from the one
    probe request it spent), and a client that reads the two counters has to
    find them there rather than having to know which branch it is on.

    Idempotent by construction: the counters are only filled in when they are
    absent, so a completed search keeps the pre-bound `results_total` this
    would otherwise overwrite with the bounded count, and `compact_row` on an
    already-compact row selects the same fields again.
    """
    rows = payload.get("results")
    if not isinstance(rows, list):
        return payload
    if compact and rows:
        payload["results"] = compact_rows(rows, roundtrip)
        rows = payload["results"]
    payload.setdefault("results_total", len(rows))
    payload.setdefault("results_returned", len(rows))
    return payload
