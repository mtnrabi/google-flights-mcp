"""
Local entrypoint: `python -m src`.

Serves streamable HTTP on /mcp, the same transport the deployment uses, so a
client configured against localhost behaves like one configured against
production. Vercel does not run this file -- it imports api/index.py.
"""

from __future__ import annotations

import logging
import sys

from .server import build_server
from .settings import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    server = build_server(settings)
    logging.getLogger("mcp_server_paid").info(
        "serving MCP on http://%s:%d/mcp (ads: never, upstream: %s)",
        settings.host,
        settings.port,
        settings.rapidapi_host,
    )
    server.run(transport="http", host=settings.host, port=settings.port, path="/mcp")


if __name__ == "__main__":
    main()
