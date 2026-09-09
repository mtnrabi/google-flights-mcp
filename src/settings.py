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


def _scoped_env_str(name: str, product: str, default: str) -> str:
    """Read ``<NAME>_<PRODUCT>`` if it is set, otherwise ``<NAME>``.

    One deployment can now answer for more than one product (see
    ``MCP_PRODUCTS_BY_HOST`` below), and two of our env vars name a specific
    listing or a specific hostname rather than the process:

    * ``MCP_PUBLIC_URL`` -- quoted back on /health as ``mcp_endpoint`` and the
      source of every policy-page link, so on a combined deployment the
      hotels host must not advertise the flights hostname.
    * ``SIGNUP_URL`` -- the RapidAPI listing a keyless caller is sent to.
      ``Settings.signup_url_for`` already keeps an explicit override from
      leaking across products, but only by ignoring it; the suffixed form
      lets both products keep an override.

    Unsuffixed names keep working exactly as before, which is what makes the
    two existing single-product deployments byte-identical under this change:
    they set neither suffixed variable, so every read falls straight through.
    """
    scoped = _env_str(f"{name}_{product.upper()}", "")
    if scoped:
        return scoped
    return _env_str(name, default)


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


#: The ``Timeout`` on the deployed ``flyMyGApi`` function behind the RapidAPI
#: host this server calls, read from the live function configuration on
#: 2026-08-27. **The only place
#: that number is written down in this project**; the wait below is derived from
#: it rather than restated, because a second literal is what let the previous
#: value go stale the day the ``Timeout`` moved.
UPSTREAM_FUNCTION_TIMEOUT_SECONDS = 60.0

#: Extra wait on top of the callee's whole life, covering connect, TLS and
#: transit between this process and the function.
#:
#: Measured through the RapidAPI edge on 2026-08-27, over 46 requests taken
#: after the ``Timeout`` was raised to 60: successful answers as late as 59.7s,
#: failures at 60.21s and 60.23s. This server calls the function directly rather
#: than through that edge, so it sees the earlier of those two -- but the same
#: rule applies, and the same margin is used so the two hops cannot be tuned
#: apart by accident.
UPSTREAM_RELAY_MARGIN_SECONDS = 15.0

#: How long this server waits on a backend call. Derived, deliberately.
#:
#: It was 45.0, matched to a ``Timeout`` of 45 on the assumption the two would
#: move together. They did not: the function was raised to 60 on 2026-08-27 and
#: this was not, which left the server abandoning searches the callee would have
#: answered -- two of the 46 measured requests came back successfully at 48.7s
#: and 59.7s, and both would have been thrown away.
#:
#: Before that it was 105, from ``backend/src/constants.py``'s deleted
#: LAMBDA_REQUEST_TIMEOUT_SECONDS (90) plus a router's +15, describing a
#: function that never existed. The rule that replaces both guesses has a
#: direction: below the callee's ``Timeout`` discards answers that were coming;
#: above it only costs latency on a request that has already failed.
#:
#: There is **no separate RapidAPI gateway ceiling near 45s**, whatever earlier
#: comments in this repo said. The 2026-08-27 measurement shows the failure wall
#: moving exactly with the function ``Timeout``.
DEFAULT_TIMEOUT_SECONDS = (
    UPSTREAM_FUNCTION_TIMEOUT_SECONDS + UPSTREAM_RELAY_MARGIN_SECONDS
)


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

    def signup_url_for(self, product: str) -> str:
        """The RapidAPI listing a caller of `product` must subscribe to.

        `signup_url` is this DEPLOYMENT's listing, which is the right answer
        for a single-product deployment and the wrong one for "both": that
        falls back to the flights listing, so a hotels caller quoted it is
        sent to a Subscribe button for an API that cannot serve them -- and
        the hotels 403 handler reads "Subscribe to the Booking Live API at
        <flights URL>".

        An explicit SIGNUP_URL still wins wherever it is the same listing this
        deployment would have used anyway, so the override is not lost.
        """
        wanted = DEFAULT_SIGNUP_URLS.get(product)
        if wanted is None or wanted == DEFAULT_SIGNUP_URLS.get(self.products):
            return self.signup_url
        return wanted

    # Which product this deployment serves: "flights", "hotels", or "both".
    #
    # One codebase, three deployments. A subscriber to the Google Flights API
    # should never be handed hotel tools that can only 403 for them -- they sit
    # in the model's tool list, cost context, and make half the server look
    # broken on first use. Config, not a fork.
    products: str = "both"

    # ── the api front ────────────────────────────────────────────────────
    # Where a source that is NOT on the RapidAPI edge is reached: today that
    # is Airbnb, whose backend (`otaLiteAgent`) is only reachable through
    # api.flightpowers.com with `provider=airbnb` in the body (flight_rabbi
    # #472). Booking is untouched by this and still goes straight to its own
    # RapidAPI host, on the caller's key, billed to their subscription.
    #
    # This holds no credential and never can: the caller's own key is
    # forwarded to the front per request, exactly as it is to RapidAPI. If
    # this server ever needed a secret of its own to reach a source, that
    # source would be billed to us rather than to the caller, which is the one
    # thing `src/providers.py` says must not happen quietly.
    api_front_base_url: str = "https://api.flightpowers.com"


VALID_PRODUCTS = ("flights", "hotels", "both")

#: Overridable so a preview deployment can point at a preview front. Never a
#: path, only an origin: the route is fixed in `providers.API_FRONT_SEARCH_PATH`.
DEFAULT_API_FRONT_BASE_URL = "https://api.flightpowers.com"

# Where a caller with no key is sent, per deployment. It is quoted back
# verbatim in `needs_api_key` replies, on /health and on the public index, so
# a hotels deployment falling back to the flights listing sends a paying user
# to a Subscribe button for the wrong API -- and the hotels 403 handler says
# "Subscribe to the Booking Live API at <that flights URL>", which is
# self-contradicting. SIGNUP_URL still overrides. "both" keeps the flights
# listing because that is the primary listing for the combined deployment.
# Mirrors legal.PRODUCT_CONTEXT[...]["SIGNUP_URL"], which fills the same slot
# on the policy pages.
DEFAULT_SIGNUP_URLS = {
    "flights": "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
    "hotels": "https://rapidapi.com/mtnrabi/api/booking-live-api",
    "both": "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
}


# Which product each public hostname sells.
#
# `google-flights-mcp` and `booking-hotels-mcp` are the same directory
# deployed twice, told apart only by MCP_PRODUCTS, and together they burn
# ~159 cold starts a day on two half-idle instances. One deployment carrying
# both hostnames keeps a single instance warm across roughly double the
# traffic. What must NOT change when they merge is what each hostname sells:
# the two Smithery listings, the two official-registry entries and the two
# RapidAPI subscriptions all describe distinct tool sets, so the product has
# to be chosen per REQUEST, from the Host header, not per process.
#
# Every custom domain the two projects actually serve, checked against DNS on
# 2026-09-04: the flights project answers on two aliases, the hotels project
# on one. There is deliberately no `booking-hotels-mcp.flightpowers.com` --
# it does not resolve, and it has already been mistaken once for a missing
# alias rather than an absent one.
#
# Anything not in this map -- a *.vercel.app deployment URL, a preview, a
# probe that sent no Host -- falls back to MCP_PRODUCTS, which is what keeps
# the two existing deployments behaving exactly as they do today.
DEFAULT_HOST_PRODUCTS = {
    "google-flights-mcp.flightpowers.com": "flights",
    "flights.flightpowers.com": "flights",
    "hotels.flightpowers.com": "hotels",
}


def normalise_host(raw: str) -> str:
    """Lowercase, strip the port, strip a trailing dot."""
    host = raw.strip().lower()
    if host.startswith("[") and "]" in host:  # IPv6 literal
        host = host[1 : host.index("]")]
    elif ":" in host:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_products() -> dict[str, str]:
    """The host -> product map for this deployment.

    ``MCP_PRODUCTS_BY_HOST`` is the knob:

    * unset, or ``default`` -- the built-in map above.
    * ``off`` (or ``none``) -- host routing disabled. One product, chosen by
      MCP_PRODUCTS, exactly as before this change. This is the rollback that
      needs no deploy: set it and redeploy and the process is what it was.
    * ``host=product,host=product`` -- an explicit map, for previews and for
      any hostname added after this file was written.

    An unrecognised product here is fatal for the same reason a mistyped
    MCP_PRODUCTS is: silently serving the wrong tool set to a paying listing
    is not a failure anyone notices quickly.
    """
    raw = _env_str("MCP_PRODUCTS_BY_HOST", "default").strip()
    if not raw or raw.lower() in ("default",):
        return dict(DEFAULT_HOST_PRODUCTS)
    if raw.lower() in ("off", "none"):
        return {}

    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        host, sep, product = pair.partition("=")
        product = product.strip().lower()
        if not sep or not host.strip() or product not in VALID_PRODUCTS:
            raise RuntimeError(
                "MCP_PRODUCTS_BY_HOST must be 'default', 'off', or "
                f"'host=product' pairs with product in {VALID_PRODUCTS}; "
                f"got {pair!r}"
            )
        mapping[normalise_host(host)] = product
    return mapping


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


def load_settings(products: str | None = None) -> Settings:
    """Settings for one product.

    ``products`` overrides MCP_PRODUCTS. A combined deployment calls this once
    per product it can answer for (see src/entrypoint.py); everything else
    calls it with no argument and reads the environment, as before.
    """
    if products is None:
        products = _products()
    elif products not in VALID_PRODUCTS:
        raise RuntimeError(
            f"products must be one of {', '.join(VALID_PRODUCTS)}, got {products!r}"
        )
    max_searches = _env_int("MAX_SEARCHES_PER_TOOL_CALL", DEFAULT_MAX_SEARCHES)
    if max_searches < 1:
        raise RuntimeError("MAX_SEARCHES_PER_TOOL_CALL must be at least 1")
    max_searches = min(max_searches, HARD_MAX_SEARCHES)

    host = _env_str("RAPIDAPI_HOST", "google-flights-live-api.p.rapidapi.com")

    return Settings(
        rapidapi_host=host,
        # Derived from the host by default so the two can never disagree.
        rapidapi_base_url=_env_str("RAPIDAPI_BASE_URL", f"https://{host}").rstrip("/"),
        # Derived; see DEFAULT_TIMEOUT_SECONDS above for the measurement and
        # for why it is never written down as a second literal.
        request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS",
                                           DEFAULT_TIMEOUT_SECONDS),
        fallback_rapidapi_key=_env_str("RAPIDAPI_KEY", ""),
        max_searches_per_tool_call=max_searches,
        max_concurrent_searches=_env_int("MAX_CONCURRENT_SEARCHES", 10),
        max_http_connections=_env_int("MAX_HTTP_CONNECTIONS", 60),
        public_url=_scoped_env_str(
            "MCP_PUBLIC_URL", products, "http://localhost:8000/mcp"
        ),
        host=_env_str("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        # Empty disables the file sink and leaves stdout MCP_CALL lines as the
        # record -- the right setting on serverless, where the filesystem is
        # read-only outside an ephemeral /tmp.
        log_path=_env_str("LOG_PATH", ""),
        # Matches TOP_N_RESULTS_PER_COMBINATION in backend/src/constants.py:25.
        default_result_limit=_env_int("DEFAULT_RESULT_LIMIT", 10),
        signup_url=_scoped_env_str(
            "SIGNUP_URL", products, DEFAULT_SIGNUP_URLS[products]
        ),
        products=products,
        api_front_base_url=_env_str(
            "API_FRONT_BASE_URL", DEFAULT_API_FRONT_BASE_URL
        ).rstrip("/"),
    )
