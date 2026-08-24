"""
Vercel entrypoint.

Vercel's Python runtime auto-discovers `api/index.py` and serves the top-level
`app` as a single Fluid function receiving every path -- so `/mcp`, `/health`,
`/metrics` and the OpenAI verification path all land in this one module. No
rewrites are needed in vercel.json.

Why a FastAPI wrapper instead of exporting the FastMCP app directly
-------------------------------------------------------------------
FastMCP's streamable-HTTP transport initialises its session manager in an ASGI
*lifespan* startup event. If the host never runs lifespan, every request to
/mcp fails with "Task group is not initialized" -- a total outage of the MCP
endpoint. Vercel's lifespan support is announced specifically for "FastAPI
apps"; whether that means FastAPI or any ASGI app is not stated. Rather than
bet the endpoint on that ambiguity, this wraps the FastMCP app in FastAPI and
hands over its lifespan, which is also exactly the integration FastMCP
documents as required when mounting into a parent ASGI app. Costs one
dependency; removes a total-failure mode. Do not remove it.

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

from fastapi import FastAPI  # noqa: E402

from src.server import SERVICE_NAME, build_server  # noqa: E402
from src.settings import load_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

_settings = load_settings()
_server = build_server(_settings)

_mcp_app = _server.http_app(stateless_http=True)

# Handing over `_mcp_app.lifespan` is the load-bearing line. Without it the
# session manager never starts and /mcp returns 500 on every request.
app = FastAPI(
    title=SERVICE_NAME,
    lifespan=_mcp_app.lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/", _mcp_app)
