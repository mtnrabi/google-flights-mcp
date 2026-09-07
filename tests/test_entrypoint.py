"""
The Vercel entrypoint for the paid servers, driven the way a host drives
it: raw ASGI.

This directory ships twice -- as `google-flights-mcp` and as
`booking-hotels-mcp` -- and one deployment can now carry both hostnames, with
the product chosen per request from the Host header. So a wrapper regression
here is two outages, not one, and the lifespan is now TWO session managers
rather than one: a parent that starts only the first leaves the other
hostname 500ing while the dashboard stays green. The per-host cases below are
what makes that visible; tests/test_host_routing.py is what pins the surface
each hostname serves.

`api/index.py` exists for one reason -- to hand FastMCP's lifespan to
whatever runs the app -- and that reason is invisible to every other test in
this suite, because `fastmcp.Client` sets the session up itself. So this file
drives the exported `app` over ASGI directly and asserts both halves:

* with the lifespan run, POST /mcp `initialize` answers with a serverInfo;
* with it skipped, the same request fails.

The second assertion is the point. It is what makes a future "the wrapper
looks redundant, let's export the FastMCP app directly" change fail here
instead of in production, where it presents as every /mcp request returning
500 "Task group is not initialized" under a green Vercel dashboard.

The wrapper is a Starlette app rather than a FastAPI one because importing
FastAPI costs ~110 ms of CPU on every cold start and this app publishes no
OpenAPI schema. Nothing here asserts the class: what matters is that the
lifespan runs and /mcp answers, which is true of either.
"""

import json

import httpx
import pytest

import api.index as entrypoint

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "entrypoint-test", "version": "1.0"},
    },
}

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _first_json_payload(body: str) -> dict:
    """The transport may answer as JSON or as a one-event SSE stream."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in response body: {body!r}")


async def test_initialize_answers_when_the_host_runs_lifespan():
    transport = httpx.ASGITransport(app=entrypoint.app)
    async with entrypoint.app.router.lifespan_context(entrypoint.app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)

    assert response.status_code == 200
    payload = _first_json_payload(response.text)
    assert payload["result"]["serverInfo"]["name"]


async def test_health_answers_when_the_host_runs_lifespan():
    transport = httpx.ASGITransport(app=entrypoint.app)
    async with entrypoint.app.router.lifespan_context(entrypoint.app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_mcp_is_dead_without_the_lifespan_wrapper():
    """Skipping lifespan is the outage this wrapper exists to prevent."""
    transport = httpx.ASGITransport(app=entrypoint.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)

    assert response.status_code >= 500, (
        "POST /mcp succeeded without the lifespan having been run, which means "
        "this test can no longer detect a wrapper that drops it"
    )


def test_the_exported_app_carries_the_mcp_lifespan():
    """`app` must be the wrapper, not the bare FastMCP app.

    Vercel loads the top-level `app` and runs its lifespan; if the two ever
    become the same object the mount below would be the thing that runs, and
    a mounted app's lifespan is not executed by the parent.
    """
    assert entrypoint.app is not entrypoint._mcp_app
    assert entrypoint.app.router.lifespan_context is not None


@pytest.mark.parametrize("attribute", ["app", "_mcp_app", "_server", "_settings"])
def test_module_exports(attribute):
    assert getattr(entrypoint, attribute) is not None


HOSTS = {
    "google-flights-mcp.flightpowers.com": "google-flights-mcp",
    "flights.flightpowers.com": "google-flights-mcp",
    "hotels.flightpowers.com": "booking-hotels-mcp",
}


@pytest.mark.parametrize("host, service", sorted(HOSTS.items()))
async def test_every_routed_host_answers_initialize(host, service):
    """One lifespan, every product's session manager.

    A parent that starts only the first child is the failure this covers: the
    hostname it forgot answers 500 "Task group is not initialized" on every
    /mcp request, under a green Vercel dashboard.
    """
    transport = httpx.ASGITransport(app=entrypoint.app)
    async with entrypoint.app.router.lifespan_context(entrypoint.app):
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://{host}"
        ) as client:
            response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
            health = await client.get("/health")

    assert response.status_code == 200
    payload = _first_json_payload(response.text)
    assert payload["result"]["serverInfo"]["name"] == service
    assert health.status_code == 200
    assert health.json()["service"] == service


@pytest.mark.parametrize("host", sorted(HOSTS))
async def test_no_routed_host_survives_a_dropped_lifespan(host):
    transport = httpx.ASGITransport(app=entrypoint.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://{host}"
    ) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)

    assert response.status_code >= 500, host
