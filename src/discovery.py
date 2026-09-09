"""
Discovery without a credential: what a caller who has not signed in may look
at, and where the 401 still stands.

Why this file exists
--------------------
On 2026-09-09 both servers began answering a credential-less `/mcp` with
`401 + WWW-Authenticate`, which is the one thing on the wire that makes an
MCP client show a Sign in button. It also broke every directory that lists
us. Glama re-checks each connector HOURLY by opening an MCP connection and
listing its tools, and that check connects with no credentials: a 401 there
marks the listing unhealthy and ranks it down, which is exactly what Glama's
mail to Matan reported the same day for both paid listings. Smithery's
release scan, mcpservers.org and M8ven probe the same way.

Both things can be true at once, because a scanner and a customer ask
different questions:

    initialize, notifications/initialized, ping, tools/list, prompts/list,
    resources/list, resources/templates/list
        -> served, no credential needed. None of these reaches a backend,
           spends a RapidAPI call, or reads or writes anything belonging to a
           user. The tool list they return is already public: it is on the
           RapidAPI listing, in every directory entry, and in the README.

    tools/call, and every other method
        -> the 401 that was there before, with the same challenge header and
           the same directions body.

So a scanner sees a healthy server with a full tool list, and the first
request that would actually spend something is still the one that asks the
caller to sign in or bring a key.

What this deliberately does NOT do
----------------------------------
* **It does not touch `/mcp/oauth`.** That alias challenges everything,
  whatever the mode. It is the URL to hand a directory that wants a server
  which always requires auth, and every connector added on it before
  2026-09-09 expects precisely that.
* **It only reads POSTs.** A GET on `/mcp` is the optional server-to-client
  stream; it carries no method to check, and it keeps the challenge, which is
  also the only thing a browser or a `curl` probe would ever see. A client
  that cannot open that stream still completes `initialize` and `tools/list`,
  which is what a health check is.
* **It fails closed.** A body that is not valid JSON, one too large to be a
  discovery request, or one naming a method not on the list, is challenged. A
  JSON-RPC batch is served only when EVERY entry in it is discovery.
* **It does not decide anything about a caller who brought a credential.**
  That request was never challenged and never reaches this file.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

#: Everything a directory scanner, a connector dialog or a curious human
#: needs to see the menu, and nothing that reads the kitchen.
#:
#: `notifications/initialized` is here because it is the message a client
#: sends immediately after `initialize`; challenging it would break the
#: handshake one message before the tool list. `ping` is here because it is
#: how several health checks decide a server is alive.
DISCOVERY_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }
)

#: A discovery POST is a few hundred bytes; an `initialize` carrying a fat
#: capabilities object is still under a kilobyte. The limit is here so that a
#: caller cannot make the gate buffer an arbitrary body before it refuses:
#: anything past it is answered with the challenge and never read.
MAX_DISCOVERY_BODY = 64 * 1024

Receive = Callable[[], Awaitable[dict]]


def is_discovery_payload(raw: bytes) -> bool:
    """True when this body contains ONLY read-only discovery calls.

    False for anything unparseable, anything empty, and any batch with one
    non-discovery entry in it -- the whole body is served or the whole body is
    challenged, because a batch is answered as one HTTP response and there is
    no status code that means "half of this".
    """
    if not raw or len(raw) > MAX_DISCOVERY_BODY:
        return False
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    entries: list[Any]
    if isinstance(parsed, dict):
        entries = [parsed]
    elif isinstance(parsed, list) and parsed:
        entries = list(parsed)
    else:
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        method = entry.get("method")
        if not isinstance(method, str) or method not in DISCOVERY_METHODS:
            return False
    return True


async def buffer_body(
    receive: Receive, limit: int = MAX_DISCOVERY_BODY
) -> tuple[bytes, dict | None]:
    """Read the whole request body off the ASGI channel.

    Returns `(body, tail)`. `tail` is a non-`http.request` message (in
    practice `http.disconnect`) that arrived instead of the rest of the body,
    kept so it can be handed to the app rather than swallowed.

    Past `limit` the accumulated bytes are dropped and `b""` comes back: the
    only caller challenges on a body that is not discovery, and an oversized
    body is never discovery, so the bytes have no reader.
    """
    body = b""
    over = False
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return (b"" if over else body), message
        chunk = message.get("body") or b""
        if not over:
            body += chunk
            if len(body) > limit:
                over = True
                body = b""
        if not message.get("more_body"):
            return (b"" if over else body), None


def replay_body(receive: Receive, body: bytes, tail: dict | None = None) -> Receive:
    """A `receive` that hands the buffered body to the app, then gets out of
    the way.

    The delegation on the third line is the load-bearing part. After a body
    is consumed, an ASGI app keeps calling `receive` to learn when the client
    goes away -- a streaming MCP response does this for the life of the
    stream. Returning a synthetic `http.disconnect` there would tell the app
    the caller had hung up and cut every reply short, so anything after the
    body comes from the real channel, which blocks until there is genuinely
    something to say.
    """
    state = {"body": False, "tail": tail is None}

    async def _receive() -> dict:
        if not state["body"]:
            state["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        if not state["tail"]:
            state["tail"] = True
            return tail  # type: ignore[return-value]
        return await receive()

    return _receive


async def discovery_probe(
    scope: Mapping[str, Any], receive: Receive | None
) -> tuple[bool, Receive | None]:
    """`(serve_it, receive)` for a credential-less request on `/mcp`.

    `serve_it` is True only for a POST whose body is discovery and nothing
    else. The returned `receive` is the one to pass on in EITHER case: the
    body has been read off the channel by then, and the app must be given it
    back or it will hang waiting for bytes that already arrived.

    A `None` receive (a unit test driving the gate by hand) is answered
    `(False, None)`: no body, nothing to serve, challenge.
    """
    if (scope.get("method") or "").upper() != "POST":
        return False, receive
    if receive is None:
        return False, None
    body, tail = await buffer_body(receive)
    return is_discovery_payload(body), replay_body(receive, body, tail)
