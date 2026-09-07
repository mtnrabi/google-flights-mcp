"""
Settings.

The load-bearing property is an absence: unlike the free server, nothing here
is required. If a future change makes some upstream secret mandatory, the
"caller brings their own key" model has quietly broken and these tests are
where that shows up.
"""

import pytest

from src.settings import (
    DEFAULT_MAX_SEARCHES,
    HARD_MAX_SEARCHES,
    load_settings,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "RAPIDAPI_HOST",
        "RAPIDAPI_BASE_URL",
        "RAPIDAPI_KEY",
        "MAX_SEARCHES_PER_TOOL_CALL",
        "MAX_CONCURRENT_SEARCHES",
        "MCP_PUBLIC_URL",
        "SIGNUP_URL",
        "REQUEST_TIMEOUT_SECONDS",
        "MCP_PRODUCTS",
    ):
        monkeypatch.delenv(name, raising=False)


class TestNothingIsRequired:
    def test_loads_with_a_completely_empty_environment(self):
        settings = load_settings()
        assert settings.rapidapi_host == "google-flights-live-api.p.rapidapi.com"

    def test_no_server_side_key_by_default(self):
        """A fallback key left set bills the deployment owner for every
        anonymous caller. Off unless someone opts in."""
        assert load_settings().fallback_rapidapi_key == ""


class TestBaseUrlDerivation:
    def test_derived_from_host_so_the_two_cannot_disagree(self):
        settings = load_settings()
        assert settings.rapidapi_base_url == f"https://{settings.rapidapi_host}"

    def test_host_override_moves_the_base_url_with_it(self, monkeypatch):
        monkeypatch.setenv("RAPIDAPI_HOST", "staging.test")
        settings = load_settings()
        assert settings.rapidapi_base_url == "https://staging.test"

    def test_explicit_base_url_wins(self, monkeypatch):
        """The route to fronting RapidAPI with a first-party domain, which is
        what unblocks the Make and Zapier directory rules."""
        monkeypatch.setenv("RAPIDAPI_BASE_URL", "https://api.flightpowers.com/")
        assert load_settings().rapidapi_base_url == "https://api.flightpowers.com"


class TestQuoting:
    def test_surrounding_quotes_are_stripped(self, monkeypatch):
        """backend/.env and apify_actor/.env both quote their values; a value
        copied across verbatim otherwise becomes an auth failure that looks
        like a wrong key rather than a quoting mistake."""
        monkeypatch.setenv("RAPIDAPI_KEY", '"quoted-key-value-long-enough"')
        assert load_settings().fallback_rapidapi_key == "quoted-key-value-long-enough"


class TestSearchCap:
    def test_default(self, monkeypatch):
        assert load_settings().max_searches_per_tool_call == DEFAULT_MAX_SEARCHES

    def test_clamped_to_the_hard_maximum(self, monkeypatch):
        monkeypatch.setenv("MAX_SEARCHES_PER_TOOL_CALL", "5000")
        assert load_settings().max_searches_per_tool_call == HARD_MAX_SEARCHES

    def test_zero_is_rejected_rather_than_silently_serving_nothing(self, monkeypatch):
        monkeypatch.setenv("MAX_SEARCHES_PER_TOOL_CALL", "0")
        with pytest.raises(RuntimeError, match="at least 1"):
            load_settings()

    def test_non_integer_is_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("MAX_SEARCHES_PER_TOOL_CALL", "lots")
        with pytest.raises(RuntimeError, match="must be an integer"):
            load_settings()

    def test_paid_cap_exceeds_the_free_servers(self):
        """The free server caps at 15 because the fan-out is our money. Here
        it is the caller's, so the ceiling is theirs to raise."""
        assert DEFAULT_MAX_SEARCHES > 15


class TestSignupUrlFollowsTheProduct:
    """Where a keyless caller is sent.

    The URL is quoted back verbatim in `needs_api_key` replies, on /health and
    on the public index. Defaulting a hotels deployment to the flights listing
    hands a paying user the Subscribe button for the wrong API -- and makes
    the hotels 403 handler say "Subscribe to the Booking Live API at
    <flights URL>", which contradicts itself.
    """

    def test_hotels_deployment_points_at_the_booking_listing(self, monkeypatch):
        monkeypatch.setenv("MCP_PRODUCTS", "hotels")
        assert load_settings().signup_url.endswith("booking-live-api")

    def test_flights_deployment_points_at_the_flights_listing(self, monkeypatch):
        monkeypatch.setenv("MCP_PRODUCTS", "flights")
        assert load_settings().signup_url.endswith("google-flights-live-api")

    def test_both_keeps_the_flights_listing(self, monkeypatch):
        monkeypatch.setenv("MCP_PRODUCTS", "both")
        assert load_settings().signup_url.endswith("google-flights-live-api")

    def test_an_explicit_signup_url_still_wins(self, monkeypatch):
        monkeypatch.setenv("MCP_PRODUCTS", "hotels")
        monkeypatch.setenv("SIGNUP_URL", "https://rapidapi.test/custom")
        assert load_settings().signup_url == "https://rapidapi.test/custom"


class TestTimeoutAlignment:
    """The read timeout cannot give up on an answer the upstream can still send.

    There is one ceiling on this hop, not two. It used to be documented as two
    -- "the search function is killed at ``Timeout: 45``, and the RapidAPI
    gateway in front of it was observed returning 502 at ~45.4s on 2026-08-25"
    -- and that second one does not exist. Measured through the edge on
    2026-08-27: at ``Timeout`` 45, 22 requests, max 45.2s, none above 45s; after
    the raise to 60, 46 requests, successful 200s at 48.7s and 59.7s and 502s at
    60.21s and 60.23s. The wall moved with the function ``Timeout``, so the ~45s
    502s recorded as a gateway ceiling were our own Lambda kill relayed by the
    edge about a quarter-second later.

    Which turns the property upside down. Stated as a ceiling it became a live
    bug the moment the ``Timeout`` was raised and the 45.0 here did not follow:
    the client abandoned searches that were about to succeed. Stated correctly
    it is a floor -- clear the function ``Timeout``, and clear the edge's relay
    of the verdict too, so the caller gets RapidAPI's specific 502 rather than a
    generic local timeout fired a fraction of a second earlier.
    """

    #: The deployed ``flyMyGApi`` ``Timeout``, read from the live configuration
    #: on 2026-08-27.
    DEPLOYED_FUNCTION_TIMEOUT_SECONDS = 60.0

    #: The slowest verdict the edge relayed in that measurement.
    SLOWEST_MEASURED_EDGE_VERDICT_SECONDS = 60.23

    def test_the_code_agrees_with_the_deployed_function_timeout(self):
        from src import settings as settings_mod

        assert (settings_mod.UPSTREAM_FUNCTION_TIMEOUT_SECONDS
                == self.DEPLOYED_FUNCTION_TIMEOUT_SECONDS)

    def test_the_default_outlives_the_upstream(self):
        assert (load_settings().request_timeout_seconds
                > self.DEPLOYED_FUNCTION_TIMEOUT_SECONDS)

    def test_the_default_also_clears_the_edge_relay(self):
        assert (load_settings().request_timeout_seconds
                > self.SLOWEST_MEASURED_EDGE_VERDICT_SECONDS)

    def test_the_default_is_derived_rather_than_restated(self):
        from src import settings as settings_mod

        assert (load_settings().request_timeout_seconds
                == settings_mod.UPSTREAM_FUNCTION_TIMEOUT_SECONDS
                + settings_mod.UPSTREAM_RELAY_MARGIN_SECONDS)

    def test_the_client_default_matches_the_settings_default(self):
        """A client constructed without settings must not be more patient.

        ``RapidApiClient`` carries its own default for direct use, and a
        divergence there is exactly how 105 survived in two places at once.
        """
        import inspect

        from src.hotels_client import HotelsClient
        from src.rapidapi_client import RapidAPIClient

        expected = load_settings().request_timeout_seconds
        for client in (RapidAPIClient, HotelsClient):
            default = inspect.signature(
                client.__init__).parameters["timeout_seconds"].default
            assert default == expected, client.__name__

    def test_it_is_still_tunable_without_a_deploy(self, monkeypatch):
        monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "20")
        assert load_settings().request_timeout_seconds == 20.0
