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
