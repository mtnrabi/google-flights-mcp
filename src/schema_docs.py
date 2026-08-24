"""
Promote the `Args:` docstring block into the JSON Schema a reviewer reads.

Every tool and prompt in this package already documents its parameters in a
Google-style `Args:` block. FastMCP does not read those blocks -- it builds
`inputSchema` from type hints alone -- so until this module existed the live
server advertised 55 parameters with a type and no description, while the
descriptions sat in the source two lines above.

That is not a cosmetic gap. Anthropic's review criteria and OpenAI's plugin
guidelines both judge whether a model can call a tool correctly from its
schema alone, and OpenAI lists missing or unclear tool metadata as a common
rejection cause. A bare `{"type": "integer"}` for `seat_type` tells a model
nothing; "1 economy, 2 premium economy, 3 business, 4 first" tells it
everything.

The fix keeps the docstring as the single source of truth rather than
duplicating every sentence into a `Field(description=...)`: `document_params`
parses the block and rewrites the function's annotations to
`Annotated[T, Field(description=...)]` *before* FastMCP inspects them. Applied
as the inner decorator:

    @mcp.tool(name="...", ...)
    @document_params
    async def search_oneway_flights(...):

Decorators run bottom-up, so the annotations are already carrying their
descriptions by the time `mcp.tool` builds the schema.

Two deliberate limits. A parameter already annotated `Annotated[...]` is left
alone, because a second `Field()` in one `Annotated` is a pydantic error rather
than a merge. And a docstring with no `Args:` block is not an error -- the
function is returned untouched -- so this can be applied uniformly without
forcing a docstring onto every helper.
"""

from __future__ import annotations

import re
import textwrap
from typing import Annotated, Any, Callable, TypeVar, get_origin, get_type_hints

from pydantic import Field

F = TypeVar("F", bound=Callable[..., Any])

# The block we read, and the sibling blocks that end it. Matched on the
# dedented line so indentation is not part of the pattern.
_ARGS_HEADER = re.compile(r"^(Args|Arguments|Parameters)\s*:$")
_OTHER_SECTION = re.compile(
    r"^(Returns?|Yields?|Raises?|Examples?|Notes?|Attributes|Warns|"
    r"See Also|References)\s*:$"
)
# "name: description" -- the name is a Python identifier, and an optional
# "(type)" between the two is tolerated because the numpy/PEP-257 habit of
# writing `limit (int): ...` is common enough to be worth not silently dropping.
_ENTRY = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(?P<text>.*)$")


def parse_args_section(docstring: str | None) -> dict[str, str]:
    """Return `{parameter: description}` from a Google-style `Args:` block.

    Continuation lines -- indented further than the parameter name -- are
    folded into one space-separated sentence, so a description wrapped across
    three source lines arrives at the client as one string rather than with
    the source's line breaks baked into it.
    """
    if not docstring:
        return {}

    lines = textwrap.dedent(docstring).splitlines()

    start = next(
        (i for i, line in enumerate(lines) if _ARGS_HEADER.match(line.strip())),
        None,
    )
    if start is None:
        return {}

    header_indent = len(lines[start]) - len(lines[start].lstrip())

    out: dict[str, str] = {}
    name: str | None = None
    parts: list[str] = []
    entry_indent: int | None = None

    def flush() -> None:
        nonlocal name, parts
        if name is not None:
            text = " ".join(p.strip() for p in parts if p.strip()).strip()
            if text:
                out[name] = text
        name, parts = None, []

    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip())
        # Back out to the header's level, or into a sibling section: the block
        # is over. Checking the indent first is what stops a *description*
        # containing the word "Returns:" from truncating the block.
        if indent <= header_indent or (
            _OTHER_SECTION.match(stripped) and indent <= (entry_indent or indent)
        ):
            break

        if entry_indent is None:
            entry_indent = indent

        if indent <= entry_indent:
            match = _ENTRY.match(stripped)
            if match:
                flush()
                name = match.group("name")
                parts = [match.group("text")]
                continue
        # Anything deeper, or a line at entry level that is not "name:", is a
        # continuation of the entry above it.
        if name is not None:
            parts.append(stripped)

    flush()
    return out


def document_params(fn: F) -> F:
    """Rewrite `fn`'s annotations so each documented parameter carries its
    description into the generated JSON Schema.

    Returns `fn` itself (mutated), so it composes with any decorator that
    inspects the signature afterwards.
    """
    docs = parse_args_section(fn.__doc__)
    if not docs:
        return fn

    # `from __future__ import annotations` is on in every module that uses
    # this, so `fn.__annotations__` holds strings; resolving them here is what
    # lets us wrap the real types. include_extras keeps any Annotated intact
    # so the guard below can see it.
    hints = get_type_hints(fn, include_extras=True)

    resolved: dict[str, Any] = {}
    for param, hint in hints.items():
        description = docs.get(param)
        if (
            param == "return"
            or description is None
            or get_origin(hint) is Annotated
        ):
            resolved[param] = hint
            continue
        resolved[param] = Annotated[hint, Field(description=description)]

    fn.__annotations__ = resolved
    return fn


def undocumented_params(schema: dict[str, Any]) -> list[str]:
    """Parameter names in an `inputSchema` that carry no description.

    Used by the tests as a standing gate: a new parameter added without a
    docstring line fails the suite instead of shipping to a reviewer.
    """
    properties = schema.get("properties") or {}
    return sorted(
        name
        for name, spec in properties.items()
        if not (isinstance(spec, dict) and spec.get("description"))
    )
