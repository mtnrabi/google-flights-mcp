"""
One deployment, two hostnames, and neither listing notices.

`google-flights-mcp` and `booking-hotels-mcp` are the same directory deployed
twice. Merging them into one Vercel project halves that pair's cold starts --
worth ~0.57 CPU-hours per 30 days, about 14% of the Hobby Fluid Active CPU cap
we are at 78% of. The risk it buys is that the merged process serves the wrong
tool set to a hostname: a flights subscriber suddenly offered hotel tools their
subscription cannot pay for, or a registry entry that stops describing what its
URL answers.

So the assertion here is not "routing works". It is that each hostname's whole
public surface -- serverInfo, instructions, every tool with its title,
annotations, input schema, output schema and parameter descriptions, and
/health -- is IDENTICAL to what that product's own deployment served before
this existed. The goldens in tests/golden/ were captured from that code with
MCP_PRODUCTS set; see tests/golden/regenerate.py.
"""

import json
import pathlib

import pytest

from src.entrypoint import build_entrypoint
from src.settings import (
    DEFAULT_HOST_PRODUCTS,
    host_products,
    load_settings,
    normalise_host,
)
from tests.golden.regenerate import capture, path_for

FLIGHTS_HOST = "google-flights-mcp.flightpowers.com"
FLIGHTS_ALIAS = "flights.flightpowers.com"
HOTELS_HOST = "hotels.flightpowers.com"

# The placeholder origins the goldens were captured against. Supplying them
# per product is itself part of what is under test: on a combined deployment a
# single MCP_PUBLIC_URL would make the hotels host advertise the flights
# hostname on /health and on every policy link.
PUBLIC_URLS = {
    "MCP_PUBLIC_URL_FLIGHTS": "https://flights.golden.test/mcp",
    "MCP_PUBLIC_URL_HOTELS": "https://hotels.golden.test/mcp",
}

MCP_ENV = (
    "MCP_PRODUCTS",
    "MCP_PRODUCTS_BY_HOST",
    "MCP_PUBLIC_URL",
    "MCP_PUBLIC_URL_FLIGHTS",
    "MCP_PUBLIC_URL_HOTELS",
    "SIGNUP_URL",
    "SIGNUP_URL_FLIGHTS",
    "SIGNUP_URL_HOTELS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No MCP_* leakage from the ambient environment into a build."""
    for name in MCP_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture(scope="module")
def combined(request):
    """The merged deployment: MCP_PRODUCTS=flights plus the built-in host map.

    Module-scoped because building it is two FastMCP constructions, and every
    test below reads the same process.
    """
    import os

    previous = {name: os.environ.get(name) for name in MCP_ENV}
    for name in MCP_ENV:
        os.environ.pop(name, None)
    os.environ["MCP_PRODUCTS"] = "flights"
    os.environ.update(PUBLIC_URLS)
    try:
        yield build_entrypoint()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def golden(product: str) -> dict:
    return json.loads(path_for(product).read_text())


class TestTheSurfaceEachHostnameServes:
    """The merge must be invisible from outside."""

    @pytest.mark.parametrize(
        "host, product",
        [
            (FLIGHTS_HOST, "flights"),
            (FLIGHTS_ALIAS, "flights"),
            (HOTELS_HOST, "hotels"),
        ],
    )
    async def test_matches_the_single_product_deployment(
        self, combined, host, product
    ):
        served = await capture(combined.app, host)
        expected = golden(product)

        # Compared piece by piece so a failure names what moved rather than
        # printing two 15 KB blobs side by side.
        assert served["serverInfo"] == expected["serverInfo"]
        assert served["instructions"] == expected["instructions"]
        assert [t["name"] for t in served["tools"]] == [
            t["name"] for t in expected["tools"]
        ]
        for got, want in zip(served["tools"], expected["tools"]):
            assert got == want, f"{want['name']} on {host}"
        assert served["health"] == expected["health"]
        assert served == expected

    async def test_the_two_hostnames_do_not_share_a_tool(self, combined):
        flights = {t["name"] for t in golden("flights")["tools"]}
        hotels = {t["name"] for t in golden("hotels")["tools"]}
        assert flights and hotels
        assert flights.isdisjoint(hotels)

    async def test_an_unknown_host_gets_the_mcp_products_fallback(self, combined):
        """A *.vercel.app URL, a preview, an uptime probe.

        This is what keeps the two existing deployments unchanged while the
        domains are still attached to them.
        """
        served = await capture(combined.app, "google-flights-abc123.vercel.app")
        assert served["serverInfo"] == golden("flights")["serverInfo"]


class TestHostSelection:
    """The dispatch itself, without paying for a build."""

    def scope(self, **headers):
        return {
            "type": "http",
            "headers": [
                (k.encode(), v.encode()) for k, v in headers.items() if v is not None
            ],
        }

    def test_host_header_picks_the_product(self, combined):
        for host, product in DEFAULT_HOST_PRODUCTS.items():
            chosen = combined.app_for_scope(self.scope(host=host))
            expected = combined.by_product.get(product)
            if expected is None:  # not built on this deployment
                continue
            assert chosen is expected, host

    def test_port_and_case_do_not_defeat_the_match(self, combined):
        chosen = combined.app_for_scope(self.scope(host="Hotels.FlightPowers.com:443"))
        assert chosen.products == "hotels"

    def test_no_host_header_falls_back(self, combined):
        assert combined.app_for_scope(self.scope()) is combined.fallback

    def test_x_forwarded_host_is_only_consulted_when_host_is_unknown(self, combined):
        """Order matters, and it is the safe way round.

        A proxy that rewrote `Host` to something internal still routes
        correctly; a client that forges `X-Forwarded-Host` cannot redirect a
        request whose `Host` we already recognised.
        """
        forwarded = combined.app_for_scope(
            self.scope(host="internal.local", **{"x-forwarded-host": HOTELS_HOST})
        )
        assert forwarded.products == "hotels"

        ignored = combined.app_for_scope(
            self.scope(host=FLIGHTS_HOST, **{"x-forwarded-host": HOTELS_HOST})
        )
        assert ignored.products == "flights"

    def test_app_for_host_accepts_a_bare_string(self, combined):
        assert combined.app_for_host(HOTELS_HOST).products == "hotels"
        assert combined.app_for_host(None) is combined.fallback
        assert combined.app_for_host("nowhere.example") is combined.fallback


class TestWhatGetsBuilt:
    """Cold-start cost is the whole reason this change exists."""

    def test_a_single_product_deployment_builds_one_server(self, clean_env):
        """`MCP_PRODUCTS_BY_HOST=off` is the no-deploy rollback.

        It must produce exactly the process this change replaced: one product,
        one FastMCP instance, one cold start's worth of construction.
        """
        clean_env.setenv("MCP_PRODUCTS", "flights")
        clean_env.setenv("MCP_PRODUCTS_BY_HOST", "off")
        entry = build_entrypoint()
        assert list(entry.by_product) == ["flights"]
        assert entry.hosts == {}
        assert entry.app_for_host(HOTELS_HOST) is entry.fallback

    def test_the_combined_deployment_builds_one_server_per_product(self, combined):
        assert sorted(combined.by_product) == ["flights", "hotels"]
        assert combined.fallback.products == "flights"

    def test_no_extra_server_when_every_host_sells_the_fallback(self, clean_env):
        clean_env.setenv("MCP_PRODUCTS", "flights")
        clean_env.setenv("MCP_PRODUCTS_BY_HOST", f"{FLIGHTS_ALIAS}=flights")
        entry = build_entrypoint()
        assert list(entry.by_product) == ["flights"]

    def test_an_unpinned_deployment_pays_for_a_third_instance(self, clean_env):
        """MCP_PRODUCTS unset means the fallback is "both", which is a THIRD
        tool set and therefore a third FastMCP construction on every cold
        start. Harmless locally, wasteful on Vercel -- so both paid projects
        set MCP_PRODUCTS explicitly, and this test is here so that stays a
        decision rather than an accident.
        """
        entry = build_entrypoint()
        assert sorted(entry.by_product) == ["both", "flights", "hotels"]
        assert entry.fallback.products == "both"

    def test_each_product_is_built_once(self, clean_env):
        """Two hostnames for one product share an instance, not duplicate it."""
        built = []

        def loader(products=None):
            settings = load_settings(products)
            built.append(settings.products)
            return settings

        clean_env.setenv("MCP_PRODUCTS", "flights")
        entry = build_entrypoint(settings_loader=loader)
        assert sorted(built) == ["flights", "hotels"]
        assert len(entry.by_product) == 2


class TestTheHostMapEnvVar:
    def test_default_is_the_built_in_map(self, clean_env):
        assert host_products() == DEFAULT_HOST_PRODUCTS
        clean_env.setenv("MCP_PRODUCTS_BY_HOST", "default")
        assert host_products() == DEFAULT_HOST_PRODUCTS

    def test_the_built_in_map_covers_both_aliases_of_both_projects(self):
        by_product = {}
        for host, product in DEFAULT_HOST_PRODUCTS.items():
            by_product.setdefault(product, []).append(host)
        assert sorted(by_product) == ["flights", "hotels"]
        assert FLIGHTS_HOST in by_product["flights"]
        assert FLIGHTS_ALIAS in by_product["flights"]
        assert by_product["hotels"] == [HOTELS_HOST]
        # Checked against DNS 2026-09-04: booking-hotels-mcp.flightpowers.com
        # does not resolve. Mapping a hostname that does not exist reads like
        # coverage and is not.
        assert "booking-hotels-mcp.flightpowers.com" not in DEFAULT_HOST_PRODUCTS
        for host in DEFAULT_HOST_PRODUCTS:
            assert host == normalise_host(host), f"{host} is not normalised"

    @pytest.mark.parametrize("value", ["off", "none", "OFF"])
    def test_off_disables_routing(self, clean_env, value):
        clean_env.setenv("MCP_PRODUCTS_BY_HOST", value)
        assert host_products() == {}

    def test_an_explicit_map_is_parsed_and_normalised(self, clean_env):
        clean_env.setenv(
            "MCP_PRODUCTS_BY_HOST",
            " Preview-One.vercel.app=hotels , preview-two.vercel.app=flights ",
        )
        assert host_products() == {
            "preview-one.vercel.app": "hotels",
            "preview-two.vercel.app": "flights",
        }

    @pytest.mark.parametrize(
        "value",
        [
            "hotels.flightpowers.com",  # no product
            "hotels.flightpowers.com=hotel",  # typo
            "=hotels",  # no host
        ],
    )
    def test_a_malformed_map_is_fatal(self, clean_env, value):
        """Same rule as MCP_PRODUCTS: a typo must not fail open.

        Quietly serving the wrong tool set to a paying listing is not the kind
        of mistake anyone notices from the outside.
        """
        clean_env.setenv("MCP_PRODUCTS_BY_HOST", value)
        with pytest.raises(RuntimeError):
            host_products()


class TestPerProductEnvOverrides:
    """`MCP_PUBLIC_URL` and `SIGNUP_URL` name a hostname and a listing."""

    def test_suffixed_value_wins_for_its_product(self, clean_env):
        clean_env.setenv("MCP_PUBLIC_URL", "https://shared.example/mcp")
        clean_env.setenv("MCP_PUBLIC_URL_HOTELS", "https://hotels.example/mcp")
        assert load_settings("hotels").public_url == "https://hotels.example/mcp"
        assert load_settings("flights").public_url == "https://shared.example/mcp"

    def test_unsuffixed_value_still_applies_to_both(self, clean_env):
        clean_env.setenv("MCP_PUBLIC_URL", "https://shared.example/mcp")
        for product in ("flights", "hotels"):
            assert load_settings(product).public_url == "https://shared.example/mcp"

    def test_a_signup_override_no_longer_has_to_be_dropped(self, clean_env):
        """Without the suffix, one SIGNUP_URL cannot serve two products.

        `signup_url_for` protects a hotels caller from a flights-only override
        by ignoring it. That is right, but it also means an override for the
        second product was impossible on a combined deployment.
        """
        clean_env.setenv("SIGNUP_URL_FLIGHTS", "https://example.test/flights")
        clean_env.setenv("SIGNUP_URL_HOTELS", "https://example.test/hotels")
        assert load_settings("flights").signup_url == "https://example.test/flights"
        assert load_settings("hotels").signup_url == "https://example.test/hotels"

    def test_defaults_are_untouched_when_nothing_is_set(self, clean_env):
        assert load_settings("hotels").signup_url.endswith("booking-live-api")
        assert load_settings("flights").signup_url.endswith("google-flights-live-api")

    def test_an_unknown_product_override_is_rejected(self, clean_env):
        with pytest.raises(RuntimeError):
            load_settings("hotel")


def test_the_goldens_are_present_and_are_two_tools_each():
    """A missing golden must fail loudly, not quietly skip the comparison."""
    for product in ("flights", "hotels"):
        path = pathlib.Path(path_for(product))
        assert path.exists(), path
        assert len(golden(product)["tools"]) == 2
