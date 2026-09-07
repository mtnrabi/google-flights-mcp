"""
Vercel entrypoint.

Vercel's Python runtime auto-discovers `api/index.py` and serves the top-level
`app` as a single Fluid function receiving every path -- so `/mcp`, `/health`,
`/metrics` and the OpenAI verification path all land in this one module. No
rewrites are needed in vercel.json.

One deployment, two hostnames
-----------------------------
This directory ships as `google-flights-mcp` AND as `booking-hotels-mcp`, and
one Vercel project can now carry both hostnames: the product is chosen per
request from the `Host` header, and each hostname keeps serving exactly the
tool set its listing promises. The routing, and the reasoning behind it, live
in `src/entrypoint.py`. A hostname this deployment does not recognise falls
back to `MCP_PRODUCTS`, which is why a single-product deployment is unchanged
by any of it.

Why a wrapper at all, and why Starlette rather than FastAPI
-----------------------------------------------------------
FastMCP's streamable-HTTP transport initialises its session manager in an
ASGI *lifespan* startup event. If the host never runs lifespan, every request
to /mcp fails with "Task group is not initialized" -- a total outage of the
MCP endpoint, verified locally by driving the app without lifespan. So the
wrapper that hands `_mcp_app.lifespan` to the host is load-bearing and must
never be removed. `tests/test_entrypoint.py` pins both halves of that.

The wrapper used to be a FastAPI app, because Vercel's lifespan support
(shipped 2025-12-09) is announced specifically for "FastAPI apps" and it was
not worth betting the endpoint on whether that meant any ASGI app. It is
Starlette now, for one reason: importing FastAPI costs ~110 ms of CPU on
every cold start (fastapi.openapi.models alone is ~80 ms of pydantic model
building, and this app serves no OpenAPI schema -- docs_url, redoc_url and
openapi_url were all None). Starlette is already imported by FastMCP, so the
same wrapper costs 0 ms.

That is safe because `class FastAPI(Starlette)` and FastAPI passes `lifespan`
straight through to `starlette.routing.Router`: there is no FastAPI-specific
lifespan mechanism for a host to special-case, so "supports lifespan events
for FastAPI apps" can only mean the ASGI adapter runs the lifespan protocol,
which is class-agnostic. `fastapi` stays in requirements.txt on purpose --
Vercel picks the framework preset from the dependency list, and a project
that resolves to `framework: null` 404s every route.

Verify after deploying: a real POST /mcp `initialize` through the public
alias. A 500 saying "Task group is not initialized" is what a host that
skips lifespan looks like.

Two other deliberate choices:

* `stateless_http=True` -- serverless invocations are short-lived and are not
  guaranteed to land on the same instance, so there is nowhere to keep a
  session. Vercel's own guidance for remote MCP is the stateless
  streamable-HTTP model.

* `json_response` left off -- buffering the whole response as JSON re-acquires
  Vercel's 4.5 MB body cap, and a 30-way flight fan-out is not comfortably
  under it. Streaming responses are exempt from that limit.

The canonical MCP path is `/mcp` with NO trailing slash.
"""

from __future__ import annotations

import logging
import os
import sys

# Vercel resolves paths against the project root but does not guarantee it is
# on sys.path for a nested entrypoint. Add it explicitly so `src` imports the
# same way locally and deployed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.entrypoint import build_entrypoint  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

_entrypoint = build_entrypoint()

# Vercel loads the top-level `app` and runs its lifespan. That lifespan starts
# EVERY mounted product app's session manager -- a mounted app's own lifespan
# is not run by its parent -- and is the load-bearing part of this module.
app = _entrypoint.app

# Kept as module-level names because they are what this file used to export
# and what a reader looks for: the fallback product's server and ASGI app,
# i.e. exactly the pair a single-product deployment serves.
_settings = _entrypoint.fallback.settings
_server = _entrypoint.fallback.server
_mcp_app = _entrypoint.fallback.mcp_app
