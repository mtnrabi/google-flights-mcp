"""Compact hotel rows, so a hotel answer costs a model what it reads.

Measured on the deployed server, 2026-10-22 17:27Z (keyed, through
hotels.flightpowers.com/mcp, `search_hotels` Lisbon 2027-03-05..08, 2 adults):
25 properties, 26,552 bytes of structuredContent and 53,543 characters for the
whole tool result (the JSON is mirrored once more as text). Per row, the
Booking link was 13,633 of the 26,552 bytes -- a session blob of tracking
parameters, the same room block id four times -- and the image CDN URL another
3,447, which no model can open. A 7-stay range on the same city came to ~35k
characters. Anthropic's directory policy 5.B asks that users be able to leave
out text they do not need; this is that, applied by default.

What a compact row keeps: every field the source sent except the image URLs,
with `link` cut to the parameters that say which stay it books (dates, party,
rooms, the highlighted room block) and `price_per_night` added where the row
carries a price and its nights. Field names are unchanged. `verbose: true` on
the tool returns the source's rows untouched, image URLs and full link
included, and `RESULT_ROWS_MAX=0` turns all of this off for the deployment.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .providers import row_total

#: Rows `search_hotels` returns when the caller names no `limit`. The source
#: sends ~25 per stay; ten is what a person compares, and the cheapest is
#: always among them (see `select_rows`).
HOTEL_DEFAULT_LIMIT = 10

#: Fields a model cannot use: image CDN URLs.
IMAGE_FIELDS: tuple[str, ...] = (
    "image_url",
    "image",
    "images",
    "photo",
    "photos",
    "thumbnail",
    "thumbnail_url",
)

#: The Booking.com link parameters that say WHICH stay the link books. The
#: rest (aid, label, srpvid, srepoch, sr_order, hpos, the room block repeated
#: under three more names, ...) are search-session tracking.
LINK_KEEP: tuple[str, ...] = (
    "checkin",
    "checkout",
    "group_adults",
    "group_children",
    "age",
    "no_rooms",
    "selected_currency",
    "highlighted_blocks",
)


def short_link(url: Any) -> Any:
    """A Booking.com link without its tracking parameters; anything else as is."""
    if not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if not (host == "booking.com" or host.endswith(".booking.com")) or not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k in LINK_KEEP
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def compact_hotel_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in IMAGE_FIELDS}
    if "link" in out:
        out["link"] = short_link(out["link"])
    nights = row.get("nights")
    total = row_total(row)
    if (
        "price_per_night" not in out
        and total is not None
        and isinstance(nights, int)
        and not isinstance(nights, bool)
        and nights > 0
    ):
        out["price_per_night"] = round(total / nights, 2)
    return out


def select_rows(rows: list[Any], limit: int) -> list[Any]:
    """The first `limit` rows in the source's order, the cheapest always kept.

    A head slice alone can cut the cheapest property (on the 17:27Z Lisbon
    search it was 10th of 25), and "the cheapest hotel" is the question most
    often asked of this tool. So when it falls past `limit` it takes the last
    kept slot; ties go to the earlier row.
    """
    if limit >= len(rows):
        return list(rows)
    kept = list(rows[:limit])
    priced = [
        (total, i)
        for i, r in enumerate(rows)
        if isinstance(r, dict) and (total := row_total(r)) is not None
    ]
    if priced and kept:
        _, cheapest = min(priced)
        if cheapest >= limit:
            kept[-1] = rows[cheapest]
    return kept
