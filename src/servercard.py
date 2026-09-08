"""
The static MCP server card: `GET /.well-known/mcp/server-card.json`.

Why it exists
-------------
Smithery scans a server to build its listing page, and a server behind an auth
wall cannot be scanned. Their publish page names exactly one way out:

    "If automatic scanning can't complete (auth wall, required configuration,
    or other issues), you can provide server metadata manually via a static
    server card at /.well-known/mcp/server-card.json"

    -- https://smithery.ai/docs/build/publish  (fetched 2026-09-08)

The URL we publish in directories is `/mcp/oauth`, which always answers 401
(that is the whole point of day 2), so a scanner reaches nothing without
completing an OAuth flow first. This document is what a scanner reads instead.
Same reasoning as `/health` and the two OAuth metadata documents: public,
unauthenticated, cheap, and produced by the process it describes so it cannot
drift from what the server actually serves.

Shape
-----
Smithery's example lists `serverInfo`, `authentication`, `tools`, `resources`
and `prompts`, and points at SEP-1649 for the full field list
(modelcontextprotocol/modelcontextprotocol#1649). This module emits the SEP
document: `$schema`, `version`, `protocolVersion`, `serverInfo`, `description`,
`documentationUrl`, `transport`, `capabilities`, `authentication`,
`instructions`, `tools`, `resources`, `prompts`, `_meta`.

Two places the SEP leaves no slot for something we need, both handled the way
the SEP itself provides for:

* `transport` is a single object, so the keyed `/mcp` endpoint -- the one every
  paying caller uses today -- goes under `_meta` as an alternative, namespaced.
  A consumer that only knows the SEP ignores it; nothing it says is needed to
  connect the published endpoint.
* `authentication` is defined as `required` + `schemes` only. The two pointers
  a client needs to actually start the flow (the protected resource and its
  RFC 9728 metadata URL) are added alongside them, named as RFC 9728 names
  them. Additional properties are not forbidden and both required fields are
  present, so a strict reader still gets a valid document.

Per host
--------
Registered once per product inside `build_server`, on that product's own
FastMCP instance, so `entrypoint.py`'s host dispatch gives it host awareness
for free: `hotels.flightpowers.com` gets the hotel card, both flights
hostnames get the flights card. Every URL in the document comes from the same
`site_origin` that `/health`, the landing page and the OAuth metadata use, so
the three cannot disagree.

The tool list is read from the LIVE registry (`mcp._list_tools()`), serialised
exactly as `tools/list` serialises it. A card with a hand-written tool list is
a listing that goes stale the first time a parameter description changes, and
nothing would fail loudly when it did.
"""

from __future__ import annotations

from typing import Any

from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .legal import PRODUCT_CONTEXT

#: RFC 8615 well-known URI from SEP-1649, and the path Smithery documents.
SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"

#: The schema and document version SEP-1649 specifies.
CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json"
CARD_VERSION = "1.0"

#: One hour, as SEP-1649 suggests. The document only changes when the server
#: is redeployed, and a scanner that re-reads it hourly is not a cost.
CARD_CACHE = "public, max-age=3600"

#: SEP-1649 requires these on the discovery endpoint so a browser-based client
#: can read it.
CARD_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "Content-Type",
}


def card_description(products: str) -> str:
    """One sentence, built from the same product table the pages use.

    Written from `PRODUCT_CONTEXT` rather than typed out again because this
    string is read by a directory and shown next to the listing: a second copy
    of "what this server is" is a second copy to keep true.
    """
    ctx = PRODUCT_CONTEXT.get(products) or PRODUCT_CONTEXT["flights"]
    return (
        f"A hosted Model Context Protocol server returning real-time "
        f"{ctx['DATA_NOUN']} data from the {ctx['UPSTREAM_API']} on RapidAPI. "
        f"Every search is billed to the caller's own subscription. No "
        f"advertising, no sponsored content and no paid placement in any tool "
        f"result."
    )


async def _wire_tools(mcp) -> list[dict[str, Any]]:
    """Every tool, serialised the way `tools/list` serialises it.

    `by_alias=True` matters: the wire name of the metadata field is `_meta`,
    and a card that spelled it `meta` would not be the same document the
    protocol returns.
    """
    tools = await mcp._list_tools()
    return [
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in tools
    ]


async def _wire_prompts(mcp) -> list[dict[str, Any]]:
    prompts = await mcp._list_prompts()
    return [
        prompt.to_mcp_prompt().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        for prompt in prompts
    ]


async def _wire_resources(mcp) -> list[dict[str, Any]]:
    resources = await mcp._list_resources()
    return [
        resource.to_mcp_resource().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        for resource in resources
    ]


def _authentication(oauth, settings, site: str) -> dict[str, Any]:
    """What a client has to do before a tool call will work.

    Two honest answers, not one:

    * OAuth configured -- the endpoint we publish is `/mcp/oauth`, it always
      challenges, so authentication is required and the scheme is oauth2. The
      resource and its metadata URL are the two values a client needs to begin,
      and they are read off `OAuthSupport` so they cannot disagree with the
      401 challenge or with the RFC 9728 document.
    * Not configured -- `/mcp` never 401s (anonymous `initialize` and
      `tools/list` are answered), so `required` is false. A tool call still
      needs the caller's own RapidAPI key; that is a per-call credential, not
      a connection-time auth scheme, and it is described under `_meta` with
      the endpoint it belongs to rather than misdeclared here.
    """
    if oauth is None:
        return {"required": False, "schemes": []}
    return {
        "required": True,
        "schemes": ["oauth2"],
        # RFC 9728 names, for the two things a client fetches next.
        "resource": oauth.resource_url,
        "resourceMetadataUrl": oauth.resource_metadata_url,
        "authorizationServers": [oauth.issuer],
    }


async def build_card(mcp, settings, oauth, site: str) -> dict[str, Any]:
    """The whole document, for one product."""
    ctx = PRODUCT_CONTEXT.get(settings.products) or PRODUCT_CONTEXT["flights"]
    site = site.rstrip("/")

    tools = await _wire_tools(mcp)
    prompts = await _wire_prompts(mcp)
    resources = await _wire_resources(mcp)

    capabilities: dict[str, Any] = {}
    if tools:
        # Static for the life of a deployment: the tool set is decided at
        # build time and nothing removes or adds one at runtime.
        capabilities["tools"] = {"listChanged": False}
    if prompts:
        capabilities["prompts"] = {"listChanged": False}
    if resources:
        capabilities["resources"] = {"listChanged": False}

    # The endpoint a directory should publish. `/mcp/oauth` where sign-in
    # exists, because that is the one a client can connect to without the
    # user pasting anything; `/mcp` otherwise.
    endpoint = "/mcp/oauth" if oauth is not None else "/mcp"

    card: dict[str, Any] = {
        "$schema": CARD_SCHEMA,
        "version": CARD_VERSION,
        # Read from the SDK, not typed here: the version this build speaks is
        # the version it should advertise.
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "serverInfo": {
            "name": mcp.name,
            "title": ctx["PRODUCT"],
            "version": mcp.version,
        },
        "description": card_description(settings.products),
        "documentationUrl": f"{site}/",
        "transport": {"type": "streamable-http", "endpoint": endpoint},
        "capabilities": capabilities,
        "authentication": _authentication(oauth, settings, site),
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
        "_meta": {
            # Namespaced, as the MCP `_meta` rules require. Everything here is
            # extra information about the same server; nothing a consumer must
            # read to connect.
            "com.flightpowers/ads": False,
            "com.flightpowers/privacyUrl": f"{site}/privacy",
            "com.flightpowers/termsUrl": f"{site}/terms",
            "com.flightpowers/supportUrl": f"{site}/support",
        },
    }

    # The keyed endpoint, listed only when it is not already the published
    # one. On a deployment with no OAuth `/mcp` IS `transport`, and repeating
    # it here would read as two ways to connect where there is one.
    if endpoint != "/mcp":
        card["_meta"]["com.flightpowers/alternativeTransports"] = [
            {
                "type": "streamable-http",
                "endpoint": "/mcp",
                "authentication": {
                    "required": False,
                    "schemes": [],
                    "description": (
                        "Bring your own RapidAPI key: send it as an "
                        "x-rapidapi-key header or as ?rapidapi_key= on the "
                        "URL. This endpoint never returns 401."
                    ),
                    "signupUrl": settings.signup_url,
                },
            }
        ]

    instructions = getattr(mcp, "instructions", None)
    if instructions:
        card["instructions"] = instructions

    return card


def register_server_card_route(mcp, settings, oauth, site: str) -> None:
    """Attach `GET /.well-known/mcp/server-card.json` to one product's app."""

    cached: dict[str, dict[str, Any]] = {}

    @mcp.custom_route(SERVER_CARD_PATH, methods=["GET"])
    async def server_card(_request: Request) -> Response:
        # Built once per process. Nothing in it varies by request -- the
        # product is chosen by which FastMCP instance the host dispatcher
        # picked, before this route is reached -- and rebuilding it would
        # re-serialise every tool schema on a cold path a scanner polls.
        if "card" not in cached:
            cached["card"] = await build_card(mcp, settings, oauth, site)
        return JSONResponse(
            cached["card"],
            headers={"Cache-Control": CARD_CACHE, **CARD_CORS},
        )
