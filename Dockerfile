# Container image for the FlightPowers travel MCP server (flights + hotels).
#
# The same server that runs at https://google-flights-mcp.flightpowers.com/mcp,
# packaged so it can be built and started anywhere -- self-hosting, and Glama's
# build test, which starts the container to verify the server actually runs.
#
# Transport: streamable HTTP on ${PORT}/mcp, which is the transport the hosted
# deployment uses, so a client pointed at this container behaves like one
# pointed at production. `python -m src` is the repo's own local entrypoint
# (src/__main__.py); api/index.py is the Vercel-specific wrapper and is not
# used here.
#
# There is NO secret in this image. Every search is billed to the caller's own
# RapidAPI subscription and their key travels with the request as the
# `x-rapidapi-key` header. The RAPIDAPI_KEY env var below is optional and
# exists only for local development: when set, a caller who sends no key of
# their own is served on -- and billed to -- whoever owns that key. Leave it
# unset unless that is what you want. `/health` reports whether it is set.
#
#   docker build -t flightpowers-mcp .
#   docker run --rm -p 8000:8000 flightpowers-mcp
#   curl http://localhost:8000/health
#
#   # optional: a server-side fallback key for local development
#   docker run --rm -p 8000:8000 -e RAPIDAPI_KEY=your_key flightpowers-mcp
#
# Get a key (free tier available):
# https://rapidapi.com/mtnrabi/api/google-flights-live-api

FROM python:3.12-slim

# PYTHONUNBUFFERED: every tool call emits one JSON line to stdout prefixed
# "MCP_CALL " -- that is the call record, and a buffered stdout loses it when
# the container is stopped.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    HOME=/app

WORKDIR /app

# Dependencies first so a source-only change does not re-resolve the tree.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# src/ is the server; legal/ holds the privacy and terms markdown that
# src/legal.py renders at /privacy and /terms (it resolves them relative to
# the package, so the directory has to be present next to src/). Directory
# reviewers follow those URLs.
COPY src/ ./src/
COPY legal/ ./legal/

# Non-root. The unprivileged user owns /app because HOME points there and
# fastmcp writes a small version-check cache under it (failures are ignored,
# but a writable home avoids the noise).
RUN useradd --create-home --home-dir /home/mcp --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

# /health is served by the same app as /mcp, needs no credential, and 200 only
# once the ASGI app is actually accepting requests.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "src"]
