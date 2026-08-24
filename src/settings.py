"""
Environment-driven configuration for the paid, ad-free flights MCP server.

The single biggest difference from the free server: this process holds no
upstream credential. Every search is billed to the *caller's* own RapidAPI
subscription, so the key arrives per request (see credentials.py) and there
is nothing here that is required for the server to boot. That is deliberate --
a missing server-side secret must never be the reason a user's own key stops
working.

There is no ad SDK, no publisher id, and no sponsored widget anywhere in this
package. That is a product decision with a hard constraint behind it: both
Anthropic's connector directory policy and OpenAI's app guidelines prohibit
advertising or sponsored content in tool results, so an ad-carrying server can
never be listed there. This one can.
"""

import os
from dataclasses import dataclass

# The caller pays per backend request, so the ceiling exists to stop a model
# burning someone's plan quota on a single over-broad question -- not to
# protect our own bill. Higher than the free server's 15 for exactly that
# reason: the spend is the user's to authorise, and they can raise it per call
# with `max_searches` up to this hard maximum.
DEFAULT_MAX_SEARCHES = 30
HARD_MAX_SEARCHES = 60


def _strip_quotes(value: str) -> str:
    """Drop one layer of surrounding quotes.

    Both existing env files in this repo (backend/.env, apify_actor/.env)
    quote their values. A value copied across verbatim would otherwise arrive
    as '"https://..."' and produce an auth failure that looks like a wrong key
    rather than a quoting mistake.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _env_str(name: str, default: str | None = None) -> str:
    raw = os.environ.get(name, default)
    value = _strip_quotes(raw) if isinstance(raw, str) else raw
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # ── Upstream (RapidAPI) ──────────────────────────────────────────────
    rapidapi_host: str
    rapidapi_base_url: str
    request_timeout_seconds: float

    # Server-side fallback key. Normally EMPTY in production: if it is set,
    # every keyless caller silently bills the owner's subscription. It exists
    # for local development and for a deliberately-funded demo deployment.
    fallback_rapidapi_key: str

    # ── Fan-out ──────────────────────────────────────────────────────────
    # Each combination in a plan is one RapidAPI request against the caller's
    # own quota, which is why the number is reported back in every response
    # rather than left for them to discover on their invoice.
    max_searches_per_tool_call: int
    max_concurrent_searches: int
    # Vercel functions share 1,024 file descriptors across every concurrent
    # execution on an instance and sockets come out of that pool, so an
    # unbounded pool plus a 30-way fan-out hits "too many open files".
    max_http_connections: int

    # ── Serving ──────────────────────────────────────────────────────────
    public_url: str
    host: str
    port: int
    log_path: str
    default_result_limit: int
    signup_url: str

    def site_origin(self) -> str:
        """The scheme+host the public pages are served from.

        Derived from `public_url` rather than configured separately, because a
        second env var is a second thing that can point somewhere the policy
        pages are not. Directory reviewers follow these URLs; a privacy link
        that 404s is a listed instant-rejection cause at Anthropic.
        """
        from urllib.parse import urlsplit

        parts = urlsplit(self.public_url)
        if not parts.scheme or not parts.netloc:
            return self.public_url.rstrip("/").removesuffix("/mcp")
        return f"{parts.scheme}://{parts.netloc}"

    # Which product this deployment serves: "flights", "hotels", or "both".
    #
    # One codebase, three deployments. A subscriber to the Google Flights API
    # should never be handed hotel tools that can only 403 for them -- they sit
    # in the model's tool list, cost context, and make half the server look
    # broken on first use. Config, not a fork.
    products: str = "both"


VALID_PRODUCTS = ("flights", "hotels", "both")


def _products() -> str:
    """Read MCP_PRODUCTS, rejecting anything unrecognised.

    A typo must not fail open. "hotels" mistyped as "hotel" quietly serving
    both products is exactly the kind of thing nobody notices until a flights
    customer asks why half the server 403s.
    """
    raw = _env_str("MCP_PRODUCTS", "both").strip().lower()
    if raw not in VALID_PRODUCTS:
        raise RuntimeError(
            f"MCP_PRODUCTS must be one of {', '.join(VALID_PRODUCTS)}, got {raw!r}"
        )
    return raw


def load_settings() -> Settings:
    max_searches = _env_int("MAX_SEARCHES_PER_TOOL_CALL", DEFAULT_MAX_SEARCHES)
    if max_searches < 1:
        raise RuntimeError("MAX_SEARCHES_PER_TOOL_CALL must be at least 1")
    max_searches = min(max_searches, HARD_MAX_SEARCHES)

    host = _env_str("RAPIDAPI_HOST", "google-flights-live-api.p.rapidapi.com")

    return Settings(
        rapidapi_host=host,
        # Derived from the host by default so the two can never disagree.
        rapidapi_base_url=_env_str("RAPIDAPI_BASE_URL", f"https://{host}").rstrip("/"),
        # The backend's own internal budget is 90s and its router allows 105s
        # (backend/src/constants.py:16, google_flights_router.py:161). Match
        # that ceiling so we never time out before the upstream does.
        request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 105.0),
        fallback_rapidapi_key=_env_str("RAPIDAPI_KEY", ""),
        max_searches_per_tool_call=max_searches,
        max_concurrent_searches=_env_int("MAX_CONCURRENT_SEARCHES", 10),
        max_http_connections=_env_int("MAX_HTTP_CONNECTIONS", 60),
        public_url=_env_str("MCP_PUBLIC_URL", "http://localhost:8000/mcp"),
        host=_env_str("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        # Empty disables the file sink and leaves stdout MCP_CALL lines as the
        # record -- the right setting on serverless, where the filesystem is
        # read-only outside an ephemeral /tmp.
        log_path=_env_str("LOG_PATH", ""),
        # Matches TOP_N_RESULTS_PER_COMBINATION in backend/src/constants.py:25.
        default_result_limit=_env_int("DEFAULT_RESULT_LIMIT", 10),
        signup_url=_env_str(
            "SIGNUP_URL",
            "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
        ),
        products=_products(),
    )
