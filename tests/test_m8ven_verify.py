"""
`/.well-known/m8ven-verify.txt`, the M8ven MCP directory's domain check.

M8ven fetches this path on the flights hostnames only
(`flights.flightpowers.com` / `google-flights-mcp.flightpowers.com`) and
expects the body `m8ven-verify=<token>` verbatim. The route is registered on
every product's FastMCP instance -- hotels included -- so what is actually
under test is that the hotels hostname never answers for a flights domain
check, and that an unconfigured deployment (M8VEN_VERIFY_TOKEN unset) cannot
be mistaken for a passing one.
"""

import os

import httpx
import pytest

import src.entrypoint as entrypoint

FLIGHTS_HOST = "google-flights-mcp.flightpowers.com"
FLIGHTS_ALIAS = "flights.flightpowers.com"
HOTELS_HOST = "hotels.flightpowers.com"

VERIFY_PATH = "/.well-known/m8ven-verify.txt"
TOKEN = "2df6e112f2c37666f49cd95e328dcaf2"

MCP_ENV = (
    "MCP_PRODUCTS",
    "MCP_PRODUCTS_BY_HOST",
    "MCP_PUBLIC_URL",
    "MCP_PUBLIC_URL_FLIGHTS",
    "MCP_PUBLIC_URL_HOTELS",
    "SIGNUP_URL",
    "SIGNUP_URL_FLIGHTS",
    "SIGNUP_URL_HOTELS",
    "M8VEN_VERIFY_TOKEN",
)
PUBLIC_URLS = {
    "MCP_PUBLIC_URL_FLIGHTS": "https://flights.golden.test/mcp",
    "MCP_PUBLIC_URL_HOTELS": "https://hotels.golden.test/mcp",
}


def _build(token: str | None):
    """The real production shape: one process, three hostnames.

    Mirrors the `combined` fixture in test_server_card.py / test_host_routing.py
    so this exercises the same entrypoint every other host-routing test does,
    rather than a single-product FastMCP instance built by hand.
    """
    previous = {name: os.environ.get(name) for name in MCP_ENV}
    for name in MCP_ENV:
        os.environ.pop(name, None)
    os.environ["MCP_PRODUCTS"] = "flights"
    os.environ.update(PUBLIC_URLS)
    if token is not None:
        os.environ["M8VEN_VERIFY_TOKEN"] = token
    try:
        yield entrypoint.build_entrypoint().app
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="module")
def configured():
    """Both flights hostnames plus hotels, M8VEN_VERIFY_TOKEN set."""
    yield from _build(TOKEN)


@pytest.fixture(scope="module")
def unconfigured():
    """Same three hostnames, M8VEN_VERIFY_TOKEN unset."""
    yield from _build(None)


async def _get(app, host: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{host}"
        ) as client:
            return await client.get(VERIFY_PATH)


class TestConfigured:
    @pytest.mark.parametrize("host", [FLIGHTS_HOST, FLIGHTS_ALIAS])
    async def test_flights_hosts_serve_the_exact_body(self, configured, host):
        resp = await _get(configured, host)
        assert resp.status_code == 200
        assert resp.text == f"m8ven-verify={TOKEN}"
        assert resp.headers["content-type"].startswith("text/plain")

    async def test_hotels_host_404s(self, configured):
        resp = await _get(configured, HOTELS_HOST)
        assert resp.status_code == 404

    async def test_an_unrecognised_host_404s(self, configured):
        """A *.vercel.app URL, a preview, a probe that sent no Host.

        Those fall back to the flights FastMCP instance (MCP_PRODUCTS=flights
        in this fixture), so the token IS configured on the instance that
        answers -- and the host check must still 404, because M8ven only
        ever fetches this on the two named flights hostnames.
        """
        resp = await _get(configured, "google-flights-abc123.vercel.app")
        assert resp.status_code == 404


class TestUnconfigured:
    @pytest.mark.parametrize(
        "host", [FLIGHTS_HOST, FLIGHTS_ALIAS, HOTELS_HOST]
    )
    async def test_every_host_404s_without_a_token(self, unconfigured, host):
        resp = await _get(unconfigured, host)
        assert resp.status_code == 404
