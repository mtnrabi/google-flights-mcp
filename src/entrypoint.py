"""
One ASGI process, one product per hostname.

Why this file exists
--------------------
`google-flights-mcp` and `booking-hotels-mcp` are the SAME directory deployed
twice on Vercel, told apart only by `MCP_PRODUCTS`. Two half-idle instances
means two sets of cold starts -- measured at ~159/day between them, at ~0.85 s
of Active CPU each -- on a team that is at 78% of the Hobby tier's 4-hour
rolling-30-day Fluid Active CPU cap. Merging them keeps one instance warm
across roughly double the traffic and cuts that pair's cold starts by at least
half: ~0.57 CPU-hours per 30 days, about 14% of the cap.

Setting `MCP_PRODUCTS=both` on one project would also merge them, and it would
be a one-line env change -- but it is a **product** change, not a cost change.
A client connected for flights would suddenly see hotel tools in `tools/list`;
the two Smithery listings and the two official-registry entries describe
distinct tool sets; the RapidAPI subscription that pays for a flight search
does not cover a hotel search. So the product is chosen per REQUEST, from the
Host header, and each hostname keeps serving exactly what its listing promises.

The shape
---------
Two `FastMCP` instances, each with its own `http_app()`, behind a tiny
host-dispatch ASGI app. Two instances rather than one filtered instance
because `tools/list` is not the only thing that differs per product: so do
`serverInfo.name`, the `instructions` string, the prompts, the policy pages
and the signup URL quoted in a keyless reply. Filtering one instance would
mean intercepting each of those; building two means every one of them is
produced by the code that already produces it today, unchanged.

Cost of the second instance: the import graph is identical (`src/server.py`
registers all four tools and then prunes, whatever the product), so the ~850 ms
that dominates a cold start is paid once. The only extra work is a second
`build_server()` -- FastMCP construction, tool registration and JSON-schema
build, measured at ~20 ms. Against ~68 s/day of saved cold starts that is not
close.

Lifespan
--------
FastMCP's streamable-HTTP transport starts its session manager in an ASGI
*lifespan* startup event; a host that never runs lifespan gets 500 "Task group
is not initialized" on every `/mcp` request. That trap now applies twice, so
the parent lifespan enters BOTH children's lifespan contexts, in the parent's
own task, and `tests/test_entrypoint.py` pins it for each host.

Deliberately NOT done: building the second instance lazily on the first
request for its hostname. A child lifespan entered inside a request task and
exited from the lifespan task crosses anyio cancel scopes, which is exactly
the class of bug the wrapper above exists to prevent. Both are built at
import.

Rollback with no code change: `MCP_PRODUCTS_BY_HOST=off` restores a
single-product process identical to the one this replaces.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

from starlette.applications import Starlette

from .oauth import OAuthResourceGate
from .server import build_server
from .settings import Settings, host_products, load_settings, normalise_host

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductApp:
    """One product's server and the ASGI app that serves it."""

    products: str
    settings: Settings
    server: Any  # FastMCP; not annotated to keep this module import-light.
    mcp_app: Any  # StarletteWithLifespan


class Entrypoint:
    """The exported app plus everything a test needs to reach behind it."""

    def __init__(
        self,
        by_product: dict[str, ProductApp],
        hosts: dict[str, str],
        fallback: str,
    ) -> None:
        self.by_product = by_product
        self.hosts = hosts
        self.fallback = by_product[fallback]
        self.app = _build_asgi_app(self)

    # ── routing ──────────────────────────────────────────────────────────

    def app_for_host(self, host: str | None) -> ProductApp:
        """The product app that answers for `host`.

        An unknown host -- a *.vercel.app deployment URL, a preview, a probe
        that sent no Host -- gets the MCP_PRODUCTS fallback. That is what
        makes this change a no-op on the two existing single-product
        deployments: nothing routes them anywhere they were not already.
        """
        if not host:
            return self.fallback
        product = self.hosts.get(normalise_host(host))
        if product is None:
            return self.fallback
        return self.by_product[product]

    def app_for_scope(self, scope: dict) -> ProductApp:
        """`Host`, then `X-Forwarded-Host`.

        `Host` first on purpose. Vercel forwards the alias the client asked
        for in `Host`, so the second lookup only ever fires for a request the
        first one did not recognise -- a proxy that rewrote `Host` to
        something internal. Reading `X-Forwarded-Host` first would let any
        client redirect a request that `Host` had already answered correctly.

        (Neither header is trusted for anything but tool selection. Both
        products are public listings served with the caller's own key, so the
        worst a forged `Host` buys is a tool list its owner could have read
        off the other hostname anyway.)
        """
        headers = dict(scope.get("headers") or ())
        for name in (b"host", b"x-forwarded-host"):
            raw = headers.get(name)
            if not raw:
                continue
            host = normalise_host(raw.decode("latin-1"))
            if host in self.hosts:
                return self.by_product[self.hosts[host]]
        return self.fallback

    # ── lifespan ─────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def lifespan(self, _app: Starlette):
        """Run every mounted child's lifespan, in this task.

        A mounted app's lifespan is NOT run by its parent, so without this
        the session managers never start and every /mcp request 500s. One
        exit stack, entered and exited in the lifespan task, so no anyio
        cancel scope crosses a task boundary.
        """
        async with contextlib.AsyncExitStack() as stack:
            for product_app in self.by_product.values():
                await stack.enter_async_context(
                    product_app.mcp_app.lifespan(product_app.mcp_app)
                )
            yield


def _build_asgi_app(entry: Entrypoint) -> Starlette:
    async def dispatch(scope, receive, send):
        await entry.app_for_scope(scope).mcp_app(scope, receive, send)

    def support_for_scope(scope):
        """The OAuth feature of whichever product answers for this Host.

        Read off the FastMCP instance rather than plumbed through
        ProductApp, because `build_server` is what decides whether the
        feature is configured and it returns a FastMCP everywhere.
        """
        return getattr(entry.app_for_scope(scope).server, "fp_oauth", None)

    app = Starlette(lifespan=entry.lifespan)
    # The gate wraps the host dispatcher rather than sitting inside a product
    # app, and it is installed UNCONDITIONALLY -- even on a deployment with
    # no OAuth configured. Two jobs:
    #
    #  1. Strip `x-fp-oauth-*` from every inbound request, whatever the path.
    #     server.py serves a user's stored RapidAPI key on the strength of
    #     that header, so a caller able to set it himself could spend a
    #     stranger's plan. Stripping it here, above everything, is what makes
    #     the injected value trustworthy -- and it must not depend on a
    #     feature flag, because the danger does not.
    #  2. Guard /mcp/oauth: 401 + the WWW-Authenticate challenge when no
    #     valid access token is presented, and otherwise rewrite the path to
    #     /mcp so the request is served by the same tools, the same product
    #     routing and the same session manager as every other call.
    app.mount("/", OAuthResourceGate(dispatch, support_for_scope))
    return app


def build_entrypoint(
    settings_loader: Callable[..., Settings] = load_settings,
) -> Entrypoint:
    """Build the process: one server per product this deployment can answer for.

    That set is the MCP_PRODUCTS fallback plus every distinct product any
    configured hostname maps to -- so a deployment whose hosts all sell what
    MCP_PRODUCTS already sells builds exactly one server, as before.
    """
    fallback_settings = settings_loader()
    hosts = host_products()

    wanted = [fallback_settings.products]
    wanted += sorted(set(hosts.values()) - {fallback_settings.products})

    by_product: dict[str, ProductApp] = {}
    for product in wanted:
        settings = (
            fallback_settings
            if product == fallback_settings.products
            else settings_loader(products=product)
        )
        server = build_server(settings)
        by_product[product] = ProductApp(
            products=product,
            settings=settings,
            server=server,
            mcp_app=server.http_app(stateless_http=True),
        )

    logger.info(
        "serving %s; hosts routed: %d",
        ", ".join(wanted),
        len(hosts),
    )
    return Entrypoint(by_product, hosts, fallback_settings.products)
