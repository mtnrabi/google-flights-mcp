"""Turn whatever a client sends as `passengers` into the list the backend reads.

Why this file exists
--------------------
The backend reads `passengers` as ONE CODE PER TRAVELLER -- Google's own
``tfs`` numbers: 1 adult, 2 child (aged 2-11), 3 infant on lap, 4 infant in
seat. So ``[1, 1, 2]`` is two adults and a child. Since flight_rabbi #511
(2026-09-26) it answers 422 to any list it cannot search as sent: a code
outside 1-4, an empty list, more than nine travellers, no adult, or more
infants on lap than adults.

Until #511 this server's own schema text said the opposite -- "Passenger
counts as [adults, children, infants]" -- and every model that read it sent a
COUNTS list: ``[2, 1, 0]`` for two adults and a child, ``[1, 0, 0]`` for one
adult. This client forwarded the list unchanged, and the backend searched it
as codes: a ``2`` became a child, a ``1`` an adult, a ``0`` was ignored, and
the caller got fares for a party they never asked about, as a 200. After #511
the same list is a 422, which the tool then reported as "Flight search is
temporarily unavailable", inviting a retry that fails the same way. On
2026-09-27 a paying MCP user got exactly that, three times in three seconds.

What it does
------------
``normalise_passengers`` answers one question: what list does the backend
get?

* ``None`` stays ``None`` (the backend searches one adult).
* A list that IS a valid codes list -- every entry 1-4, at least one adult,
  at most nine -- is forwarded unchanged. ``[1, 2]`` is one adult and one
  child, as the schema says, even though a caller thinking in counts might
  have meant one adult and two children: the documented reading wins, and
  it is the same party the backend would have searched before this file.
* A list that CANNOT be codes but reads cleanly as counts -- one to four
  entries ``[adults, children, infants on lap, infants in seat]``, each a
  whole number, at least one adult, one to nine travellers in total -- is
  expanded: ``[2, 1, 0]`` becomes ``[1, 1, 2]``, ``[2]`` becomes ``[1, 1]``.
  A list is "not codes" when it holds a 0 or a number above 4, or has no 1
  in it (``[2]`` as codes is a child travelling alone, which Google turns
  into "2 passengers" and the backend refuses; as a count it is what every
  model that sends it means, two adults).
* Anything else -- ``[]``, ``[0, 1]``, a negative number, ten travellers, a
  codes list with more lap infants than adults -- is forwarded unchanged so
  the backend's own 422 names the field and spells out the codes. This
  file never invents a party it cannot justify; the backend stays the single
  authority on what can be searched.

The helper is pure and typed loosely on purpose: FastMCP has already
validated the argument against ``list[int] | None`` by the time it runs, so
a string or a bare integer never reaches it from a tool call, but a unit test
or a future caller may hand it anything and gets the same list back.
"""

from __future__ import annotations

from typing import Any

#: Google's passenger codes, in the order a counts list names them.
ADULT = 1
CHILD = 2
INFANT_ON_LAP = 3
INFANT_IN_SEAT = 4
PASSENGER_CODES = (ADULT, CHILD, INFANT_ON_LAP, INFANT_IN_SEAT)

#: The backend's cap on travellers in one search (Google opens a longer list
#: as one adult; the backend refuses it instead).
MAX_TRAVELLERS = 9

#: The most entries a counts list can have: adults, children, infants on
#: lap, infants in seat. Anything longer is not a counts list.
_MAX_COUNT_ENTRIES = len(PASSENGER_CODES)


def _whole_numbers(value: Any) -> list[int] | None:
    """`value` as a list of ints, or None if it is not one.

    `bool` is an `int` in Python, so `[True, False]` would otherwise read as
    `[1, 0]`; a client that sends booleans here has a different bug, and
    silently searching one adult for it would hide that bug.
    """
    if not isinstance(value, (list, tuple)):
        return None
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        out.append(item)
    return out


def is_codes_list(entries: list[int]) -> bool:
    """True when the backend would read `entries` as a party of travellers.

    Mirrors the backend's own rules except the lap-infant one (each infant on
    lap needs its own adult), which stays the backend's to refuse: a list
    like `[1, 3, 3]` is unmistakably codes and unmistakably wrong, and the
    422 that names the field is the right answer for it.
    """
    return (
        0 < len(entries) <= MAX_TRAVELLERS
        and all(code in PASSENGER_CODES for code in entries)
        and ADULT in entries
    )


def counts_to_codes(entries: list[int]) -> list[int] | None:
    """Expand `[adults, children, infants on lap, infants in seat]` to codes.

    None when `entries` does not read as counts: more than four entries, a
    negative number, no adult, or a party of more than nine travellers. The
    missing trailing entries are zero, so `[2]` is two adults and `[2, 1]` is
    two adults and a child.
    """
    if not 0 < len(entries) <= _MAX_COUNT_ENTRIES:
        return None
    if any(count < 0 for count in entries):
        return None
    adults = entries[0]
    if adults < 1:
        return None
    total = sum(entries)
    if not 0 < total <= MAX_TRAVELLERS:
        return None
    codes: list[int] = []
    for code, count in zip(PASSENGER_CODES, entries):
        codes.extend([code] * count)
    return codes


def normalise_passengers(value: Any) -> Any:
    """The `passengers` list to send to the backend for what the client sent.

    See the module docstring for the rules. Returns `value` itself for
    anything it does not recognise, so the caller's own validation (and the
    backend's 422) see exactly what the client sent.
    """
    if value is None:
        return None
    entries = _whole_numbers(value)
    if entries is None:
        return value
    if is_codes_list(entries):
        return value
    expanded = counts_to_codes(entries)
    if expanded is not None:
        return expanded
    return value


#: The status prefixes the upstream client puts in its error string when the
#: backend REFUSED the request rather than failed to serve it. The client
#: formats every non-200 as ``HTTP <status>: <detail>`` (see the search
#: client next to this file), so the string is the one place the status
#: survives the fan-out.
_REJECT_MARKERS = ("HTTP 422:", "HTTP 400:")


def is_upstream_reject(error: str | None) -> bool:
    """True when a fan-out's `first_error` is the backend refusing the input.

    A 422 is not an outage: the request was read, judged and turned down with
    a sentence that names the field. Telling the model "temporarily
    unavailable" for it invites a retry that fails identically; telling it
    the request was refused invites the fix.
    """
    if not error:
        return False
    return any(marker in error for marker in _REJECT_MARKERS)


def refusal_message(first_error: str) -> str:
    """The tool error for a search the backend refused on every combination."""
    return (
        f"The search request was refused, not searched ({first_error}). "
        "Fix the argument named there and call again; retrying it unchanged "
        "fails the same way, and nothing was found because nothing was searched."
    )
