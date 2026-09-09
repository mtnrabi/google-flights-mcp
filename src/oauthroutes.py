"""
The HTTP surface of MCP-protocol OAuth: metadata, registration, authorize,
token, revoke.

Split out of server.py rather than added to it. `/connect`'s five routes live
in `build_server` because they were five; these are nine, and the file they
would join is already 2,400 lines. Everything here is registration and
plumbing -- the decisions are in `src/oauth.py`, and this module should read
as the shortest possible path from a Starlette request to one of them.

Route map (all on both product hostnames):

    GET  /.well-known/oauth-protected-resource            RFC 9728
    GET  /.well-known/oauth-protected-resource/mcp/oauth  (path-scoped form)
    GET  /.well-known/oauth-authorization-server          RFC 8414
    GET  /.well-known/oauth-authorization-server/mcp/oauth
    POST /oauth/register                                  RFC 7591 (DCR)
    GET  /connect/authorize                               the consent page
    POST /connect/authorize                               approve / deny
    POST /oauth/token                                     RFC 6749 §4.1.3, §6
    POST /oauth/revoke                                    RFC 7009

`/mcp/oauth` itself is NOT here: it is an ASGI-level gate
(`oauth.OAuthResourceGate`) wired in `src/entrypoint.py`, because it has to
strip injected headers before any route runs and rewrite the path to `/mcp`
so the request is served by the same tool registry `/mcp` serves.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .keystore import KeyStoreError
from .legal import page
from .oauth import (
    AUTHORIZATION_SERVER_PATH,
    AUTHORIZE_PATH,
    MCP_OAUTH_PATH,
    PROTECTED_RESOURCE_PATH,
    REGISTER_PATH,
    REVOKE_PATH,
    TOKEN_PATH,
    OAuthError,
    OAuthSupport,
    cancelled_html,
    consent_html,
    error_html,
    is_loopback_redirect,
    redirect_with,
)
from . import cimd
from .oauthstore import OAuthStoreError
from .ratelimit import (
    CIMD_FETCH,
    LIMITER,
    REGISTER,
    TOKEN,
    UNKNOWN_IP,
    Limit,
    client_ip,
    usable_address,
)
from .webauth import SESSION_COOKIE, WebAuthError

logger = logging.getLogger(__name__)


def _too_many(limit: Limit) -> JSONResponse:
    """The 429 both public POST endpoints answer with when they are flooded.

    `Retry-After` is not decoration: an MCP client that gets a bare 429 with
    no interval retries immediately, which is the behaviour the limit exists
    to stop.
    """
    return JSONResponse(
        {
            "error": "temporarily_unavailable",
            "error_description": (
                "too many requests to this endpoint; wait and try again"
            ),
        },
        status_code=429,
        headers={
            "Retry-After": str(limit.retry_after()),
            "Cache-Control": "no-store",
        },
    )


def _bounce(
    redirect_uri: str,
    params: dict[str, str],
    *,
    status: int,
    title: str,
    lead: str,
) -> Response:
    """Send the error back to the client -- unless nobody can receive it.

    RFC 6749 §4.1.2.1 says an error on a registered `redirect_uri` goes back
    to the client, and for a hosted client that is right: their page can say
    "you declined" far better than we can. For a loopback callback it is a
    coin flip, and the losing side is Chrome's own "site can't be reached"
    error on a user who has just pressed Deny -- which is what a real user
    hit on 2026-09-09. `is_loopback_redirect` documents why that is the only
    case we second-guess; the redirect is still one click away on the page.
    """
    target = redirect_with(redirect_uri, params)
    if is_loopback_redirect(redirect_uri):
        return HTMLResponse(
            page(title, cancelled_html(target, title=title, lead=lead)),
            status_code=200,
        )
    return RedirectResponse(target, status_code=status)


def caller_ip(request: Request) -> str:
    """The bucket key for this request: see `ratelimit.client_ip`.

    The direct-peer fallback is only reached when the platform sent neither
    header (a local uvicorn, a test), and it is filtered through the same
    predicate, so a proxy chain can never talk us into an address by leaving
    the headers off.
    """
    ip = client_ip({k.lower(): v for k, v in request.headers.items()})
    if ip == UNKNOWN_IP and request.client is not None:
        ip = usable_address(request.client.host or "") or UNKNOWN_IP
    return ip


def _rate_limited(request: Request, limit: Limit) -> JSONResponse | None:
    """None when the request may proceed, a 429 when it may not."""
    ip = caller_ip(request)
    if LIMITER.allow(limit, ip):
        return None
    logger.warning("rate limit hit on %s by %s", limit.name, ip)
    return _too_many(limit)

#: Metadata is public, immutable per deployment, and polled by every client
#: on every connect. Five minutes of caching is the difference between a
#: cold start per client and a cold start per client per hour.
_METADATA_CACHE = "public, max-age=300"


def _no_store(body: dict[str, Any], status: int = 200) -> JSONResponse:
    """Token responses must not be cached. RFC 6749 §5.1 is explicit."""
    return JSONResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _form(request: Request) -> dict[str, str]:
    """A token/revoke request body, from a form or from JSON.

    The RFC says form-encoded. Some MCP clients send JSON anyway, and a
    server that answers `invalid_request` to a correct-but-JSON token request
    produces a support thread rather than a bug report, so both are read.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError:
            return {}
        return {k: str(v) for k, v in body.items()} if isinstance(body, dict) else {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def register_oauth_routes(mcp, oauth: OAuthSupport, settings, connect) -> None:
    """Attach every OAuth route to one product's FastMCP instance."""

    _canonical_host = oauth.issuer.split("://", 1)[-1].rstrip("/").lower()

    def _wrong_host(request: Request) -> Response | None:
        """Bounce an alias hostname to the canonical origin, query intact.

        Same reasoning as /connect's version, and the same necessity: the
        session cookie is per host and Google compares `redirect_uri`
        literally, so an authorization that starts on `flights.` and returns
        to `google-flights-mcp.` finds no session. Doing it here means the
        whole OAuth flow -- including the metadata a client already fetched --
        stays on one origin.
        """
        host = (request.headers.get("host") or "").strip().lower()
        if not host or host == _canonical_host:
            return None
        query = request.url.query
        target = f"{oauth.issuer}{request.url.path}" + (f"?{query}" if query else "")
        return RedirectResponse(target, status_code=302)

    # ── discovery ────────────────────────────────────────────────────────

    async def _protected_resource(_request: Request) -> Response:
        return JSONResponse(
            oauth.protected_resource_metadata(),
            headers={"Cache-Control": _METADATA_CACHE},
        )

    async def _authorization_server(_request: Request) -> Response:
        return JSONResponse(
            oauth.authorization_server_metadata(),
            headers={"Cache-Control": _METADATA_CACHE},
        )

    # Both the bare and the path-scoped form. RFC 9728 §3.1 says a client
    # inserts the resource's path between the well-known segment and nothing
    # else, so `/mcp/oauth` is where a client that read our 401 challenge
    # looks; the bare path is where a client that only knows the origin
    # looks. Serving one and not the other is a discovery failure that
    # presents as "this server does not support sign-in".
    for path in (
        PROTECTED_RESOURCE_PATH,
        f"{PROTECTED_RESOURCE_PATH}{MCP_OAUTH_PATH}",
    ):
        mcp.custom_route(path, methods=["GET"])(_protected_resource)
    for path in (
        AUTHORIZATION_SERVER_PATH,
        f"{AUTHORIZATION_SERVER_PATH}{MCP_OAUTH_PATH}",
    ):
        mcp.custom_route(path, methods=["GET"])(_authorization_server)

    # ── dynamic client registration ──────────────────────────────────────

    @mcp.custom_route(REGISTER_PATH, methods=["POST"])
    async def register(request: Request) -> Response:
        # Two limits, in cheapness order: the per-instance rate limit costs a
        # dictionary lookup, the durable per-day cap inside `oauth.register`
        # costs a query.
        throttled = _rate_limited(request, REGISTER)
        if throttled is not None:
            return throttled
        try:
            body = await request.json()
        except ValueError:
            return _no_store(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "the request body must be JSON",
                },
                400,
            )
        if not isinstance(body, dict):
            return _no_store(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "the request body must be a JSON object",
                },
                400,
            )
        try:
            response = await oauth.register(body, ip=caller_ip(request))
        except OAuthError as exc:
            if exc.retry_after:
                return JSONResponse(
                    exc.as_dict(),
                    status_code=exc.status,
                    headers={
                        "Retry-After": str(exc.retry_after),
                        "Cache-Control": "no-store",
                    },
                )
            return _no_store(exc.as_dict(), exc.status)
        except OAuthStoreError as exc:
            logger.warning("client registration failed: %s", exc)
            return _no_store(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "the registration store is not reachable",
                },
                503,
            )
        return _no_store(response, 201)

    # ── the authorization endpoint ───────────────────────────────────────

    def _error_page(title: str, message: str, status: int = 400) -> Response:
        return HTMLResponse(page(title, error_html(title, message)), status_code=status)

    @mcp.custom_route(AUTHORIZE_PATH, methods=["GET"])
    async def authorize(request: Request) -> Response:
        redirect = _wrong_host(request)
        if redirect is not None:
            return redirect

        params = dict(request.query_params)
        # A CIMD client_id is a URL this server fetches, and this route is
        # reachable without signing in. Rate limited on its own so it cannot
        # be used as an anonymous fetcher; a registered `fpcl_` client never
        # reaches the network and is never limited here.
        if cimd.is_cimd_client_id((params.get("client_id") or "").strip()):
            if not LIMITER.allow(CIMD_FETCH, caller_ip(request)):
                logger.warning(
                    "rate limit hit on the CIMD lookup by %s", caller_ip(request)
                )
                return _error_page(
                    "Too many sign-in attempts",
                    "That is a lot of sign-in requests from one place in a "
                    "short time. Nothing was approved; wait a few minutes "
                    "and try again.",
                    429,
                )
        try:
            client, validated = await oauth.read_authorize_request(params)
        except OAuthError as exc:
            if not exc.redirectable:
                return _error_page(
                    "That sign-in request cannot be completed",
                    exc.description or exc.code,
                    exc.status,
                )
            # Past this point the redirect_uri has been checked against the
            # registration, so bouncing the error back is the RFC's answer
            # and is what lets the client show the user something useful.
            return _bounce(
                (params.get("redirect_uri") or "").strip(),
                {
                    "error": exc.code,
                    "error_description": exc.description,
                    "state": params.get("state") or "",
                },
                status=302,
                title="That sign-in could not be completed",
                lead=(
                    "Your client sent a request this server could not accept: "
                    + (exc.description or exc.code)
                ),
            )

        identity = connect.auth.read_session(request.cookies.get(SESSION_COOKIE))
        if identity is None:
            # Hand off to the day-1 Google sign-in and come straight back
            # here. `next` rides inside the signed state cookie, so it cannot
            # be edited into an open redirect.
            return RedirectResponse(
                f"/connect/start?{urlencode({'next': f'{AUTHORIZE_PATH}?{urlencode(params)}'})}",
                status_code=302,
            )

        try:
            summary = await connect.store.summary(identity.sub)
        except KeyStoreError as exc:
            # A store outage must not silently look like "no key connected":
            # the consent page would tell the user to go and connect a key
            # they already have.
            logger.warning("consent page could not read the key store: %s", exc)
            return _error_page(
                "Not right now",
                "The key store is not reachable at the moment. Nothing was "
                "approved; try again in a minute.",
                503,
            )

        # This registration is now in front of a human, which is as good a
        # reason to keep the row as the approval that may follow it: the
        # sweep deletes registrations that never got this far, and a client
        # that registered at install and is signing in days later must not
        # be deleted while the consent page is on screen. Best effort -- a
        # store hiccup here must not stop the page rendering.
        await oauth.note_consent_shown(client.client_id)

        return HTMLResponse(
            page(
                f"Connect {client.client_name}?",
                consent_html(
                    client_name=client.client_name,
                    client_id=client.client_id,
                    redirect_uri=validated["redirect_uri"],
                    email=identity.email,
                    product=settings.products,
                    sealed=oauth.seal_request(validated, identity.sub),
                    csrf=connect.csrf(identity.sub),
                    has_key=summary is not None,
                    connect_url=f"{oauth.issuer}/connect",
                ),
            )
        )

    @mcp.custom_route(AUTHORIZE_PATH, methods=["POST"])
    async def authorize_decision(request: Request) -> Response:
        identity = connect.auth.read_session(request.cookies.get(SESSION_COOKIE))
        if identity is None:
            return _error_page(
                "Your sign-in expired",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )
        form = await request.form()
        if not connect.csrf_ok(identity.sub, str(form.get("csrf", ""))):
            return _error_page(
                "That form had expired",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )
        try:
            validated = oauth.open_request(str(form.get("request", "")), identity.sub)
        except WebAuthError as exc:
            logger.info("consent form rejected: %s", exc)
            return _error_page(
                "That approval could not be read",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )

        state = validated.get("state", "")
        if str(form.get("decision", "")) != "approve":
            return _bounce(
                validated["redirect_uri"],
                {
                    "error": "access_denied",
                    "error_description": "the user declined",
                    "state": state,
                },
                status=303,
                title="You cancelled",
                lead=(
                    "No access was given. That client cannot search on your "
                    "behalf."
                ),
            )

        try:
            code = await oauth.issue_code(
                validated, identity.sub, email=identity.email
            )
        except OAuthStoreError as exc:
            logger.warning("could not issue an authorization code: %s", exc)
            return _error_page(
                "Not right now",
                "The sign-in store is not reachable at the moment. Nothing "
                "was approved; try again in a minute.",
                503,
            )
        logger.info(
            "authorized client %s for sub=%s", validated["client_id"], identity.sub
        )
        return RedirectResponse(
            redirect_with(validated["redirect_uri"], {"code": code, "state": state}),
            status_code=303,
        )

    # ── token and revocation ─────────────────────────────────────────────

    @mcp.custom_route(TOKEN_PATH, methods=["POST"])
    async def token(request: Request) -> Response:
        throttled = _rate_limited(request, TOKEN)
        if throttled is not None:
            return throttled
        form = await _form(request)
        try:
            issued = await oauth.token(form, request.headers.get("authorization"))
        except OAuthError as exc:
            headers = (
                {"WWW-Authenticate": 'Basic realm="mcp"'}
                if exc.status == 401 and exc.code == "invalid_client"
                else {}
            )
            return JSONResponse(
                exc.as_dict(),
                status_code=exc.status,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache", **headers},
            )
        except OAuthStoreError as exc:
            logger.warning("token endpoint could not reach the store: %s", exc)
            return _no_store(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "the sign-in store is not reachable",
                },
                503,
            )
        return _no_store(issued)

    @mcp.custom_route(REVOKE_PATH, methods=["POST"])
    async def revoke(request: Request) -> Response:
        form = await _form(request)
        await oauth.revoke(form, request.headers.get("authorization"))
        # RFC 7009 §2.2: 200 with an empty body, whatever happened.
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    logger.info(
        "MCP OAuth is enabled for %s at %s (issued %s)",
        settings.products,
        oauth.resource_url,
        time.strftime("%Y-%m-%d", time.gmtime()),
    )
