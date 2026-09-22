"""Per-request `_meta.ui.domain` on the flights widget resource.

WHY THIS EXISTS
---------------
claude.ai computes ``sha256(<the connector URL the user added>)[:32] +
".claudemcpcontent.com"`` and refuses to render the frame when the
resource advertises anything else. The user sees a red "Unable to reach
FlightPowers" chip beside a perfectly good answer and nothing is logged
anywhere. On 2026-09-15 that state had been live on the free server since
launch, because `MCP_PUBLIC_URL` was the bare origin and the hash was one
path segment out.

The domain is baked into the resource at REGISTRATION time, once per
process. This deployment answers on more than one hostname --
`google-flights-mcp.flightpowers.com` and `flights.flightpowers.com` both
sell flights, `hotels.flightpowers.com` sells hotels, and every preview
gets a `*.vercel.app` name -- so a single baked-in value is right for
exactly one population and silently broken for the rest. The rewrite
therefore happens on the way out, per request, here: the same question
the OAuth issuer already answers from the Host header, answered the same
way.

WHAT IT TOUCHES
---------------
Only ``meta["ui"]["domain"]``, and only when the value already looks like
an MCP Apps content domain. A resource with no `ui` meta, or a domain
somebody set by hand, is handed back untouched, and the registry objects
are never mutated -- `model_copy` gives each response its own view,
because the registry is shared by every concurrent request on the
instance.

Both `resources/list` AND `resources/read` carry the meta, and claude.ai
validates on the READ, so a middleware that only handled the list would
leave the red chip exactly where it was (the free server shipped that bug
and had to fix it). Both are covered. A request with no HTTP scope -- an
in-memory client in the test suite, stdio -- gets the canonical URL's
domain, which is what a single-hostname deployment always got.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import Middleware

from .widget import (
    APPS_DOMAIN_SUFFIX,
    MCP_MOUNT_PATH,
    ORIGINAL_PATH_SCOPE_KEY,
    canonical_connector_url,
    claude_apps_domain,
)

logger = logging.getLogger(__name__)


def _http_request() -> Any:
    """The live HTTP request, or None when there is not one."""
    try:
        from fastmcp.server.dependencies import (  # noqa: PLC0415
            get_http_request,
        )

        return get_http_request()
    except Exception:  # noqa: BLE001 - no HTTP request is a normal state
        return None


def connector_url_from_request(request: Any, fallback: str) -> str:
    """The URL the caller connected to, as they typed it.

    Derived from the request rather than from a configured list of
    aliases, because the set of hostnames is not closed: the two
    production aliases, every Vercel preview, and localhost during
    development all have to hash to themselves or the card does not
    render for any of them.

    * host comes from `Host`, then `X-Forwarded-Host` -- the same order
      and the same reason as `entrypoint.app_for_scope`: Vercel forwards
      the alias the client asked for in `Host`, so the second lookup only
      fires for a proxy that rewrote it.
    * scheme comes from `X-Forwarded-Proto`, defaulting to https, because
      the function never sees the TLS termination in front of it.
    * the path is the one the CLIENT asked for, with a trailing slash
      dropped ("/mcp/" hashes differently from "/mcp", and every guide,
      listing and reply we have ever printed says "/mcp"). An empty path
      falls back to the mount path.

      "The one the client asked for" is not the same as
      `request.url.path`. `oauth.OAuthResourceGate` rewrites `/mcp/oauth`
      to `/mcp` before the request reaches FastMCP -- it has to, because
      FastMCP mounts a plain `Route("/mcp")`. So a caller who connected on
      the `/mcp/oauth` alias, which is the URL in every printed guide and
      in every connector added before 2026-09-09, arrives here looking
      like a `/mcp` caller, and hashing `/mcp` would hand claude.ai a
      domain one segment out: card never renders, nothing logged. The gate
      stashes the real path under `ORIGINAL_PATH_SCOPE_KEY` and it is read
      first here.

    A request with no Host at all -- a probe, a test client -- gets
    `fallback`, which is the canonical public URL.
    """
    if request is None:
        return fallback
    try:
        headers = request.headers
        host = (headers.get("host") or headers.get("x-forwarded-host") or "").strip()
        if not host:
            return fallback
        # A comma-separated X-Forwarded-Host carries the original first.
        host = host.split(",")[0].strip().rstrip(".").lower()
        if not host:
            return fallback
        scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if scheme not in ("http", "https"):
            scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]") else "https"
        scope = getattr(request, "scope", None) or {}
        original = scope.get(ORIGINAL_PATH_SCOPE_KEY)
        raw_path = original if isinstance(original, str) and original else request.url.path
        path = (raw_path or "").rstrip("/") or MCP_MOUNT_PATH
        return f"{scheme}://{host}{path}"
    except Exception:  # noqa: BLE001 - never break a search over a header
        return fallback


class HostWidgetDomainMiddleware(Middleware):
    """Rewrites `ui.domain` to match the hostname the caller connected to."""

    def __init__(self, canonical_url: str) -> None:
        #: Normalised here so a `MCP_PUBLIC_URL` set to a bare origin --
        #: the 2026-09-15 shape -- cannot reach the hash.
        self._fallback = canonical_connector_url(canonical_url)
        self._cache: dict[str, str] = {}

    # ── the hash ─────────────────────────────────────────────────────────

    def domain_for_request(self) -> str:
        url = connector_url_from_request(_http_request(), self._fallback)
        cached = self._cache.get(url)
        if cached is None:
            cached = claude_apps_domain(url)
            # Bounded: a hostile Host header must not grow this without
            # limit on a warm instance. The real set is three entries.
            if len(self._cache) < 64:
                self._cache[url] = cached
        return cached

    # ── the rewrite ──────────────────────────────────────────────────────

    def _retarget(self, meta: Any, domain: str) -> dict | None:
        """A copy of `meta` with our `ui.domain` replaced, or None for no change."""
        if not isinstance(meta, dict):
            return None
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return None
        current = ui.get("domain")
        if not isinstance(current, str) or not current.endswith(APPS_DOMAIN_SUFFIX):
            return None
        if current == domain:
            return None
        out = dict(meta)
        out["ui"] = {**ui, "domain": domain}
        return out

    async def on_list_resources(self, context, call_next):
        resources = await call_next(context)
        domain = self.domain_for_request()
        out = []
        for resource in resources:
            meta = self._retarget(getattr(resource, "meta", None), domain)
            out.append(resource if meta is None else resource.model_copy(update={"meta": meta}))
        return out

    def _retarget_blocks(self, blocks, domain: str):
        """`(rewritten blocks, changed?)`, leaving untouched blocks as they are."""
        out = []
        changed = False
        for block in blocks:
            meta = self._retarget(getattr(block, "meta", None), domain)
            if meta is None:
                out.append(block)
                continue
            changed = True
            out.append(block.model_copy(update={"meta": meta}))
        return out, changed

    async def on_read_resource(self, context, call_next):
        """`resources/read` carries the meta too -- and it is the call
        claude.ai validates on, so handling only the list would leave the
        red chip exactly where it was.

        fastmcp 3.4.7 hands this hook a `ResourceResult` (a pydantic model
        with `contents` and its own `meta`), not a list. Both shapes are
        handled: nothing guarantees the hook's type across a version.
        """
        result = await call_next(context)
        domain = self.domain_for_request()

        contents = getattr(result, "contents", None)
        if contents is not None and not isinstance(result, (list, tuple)):
            blocks, changed = self._retarget_blocks(contents, domain)
            meta = self._retarget(getattr(result, "meta", None), domain)
            if not changed and meta is None:
                return result
            update: dict[str, Any] = {"contents": blocks}
            if meta is not None:
                update["meta"] = meta
            return result.model_copy(update=update)

        if isinstance(result, (list, tuple)):
            blocks, changed = self._retarget_blocks(result, domain)
            if not changed:
                return result
            return tuple(blocks) if isinstance(result, tuple) else blocks

        return result
