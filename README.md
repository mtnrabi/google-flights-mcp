# Google Flights MCP — real-time fares your agent can search across a whole date range, ad-free

```bash
claude mcp add --transport http google-flights https://google-flights-mcp.flightpowers.com/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```

Hosted. Nothing to clone, nothing to build. Listed in the official MCP Registry as
`com.flightpowers/google-flights-mcp`. Health check:
[`/health`](https://google-flights-mcp.flightpowers.com/health).

**Need a key?** Subscribe to the Google Flights Live API on RapidAPI — free tier available —
and copy your `x-rapidapi-key`: **https://rapidapi.com/mtnrabi/api/google-flights-live-api**

**No key yet? Start with the free server — same search, no signup:**
`claude mcp add --transport http google-flights-free https://google-flights-lulu.flightpowers.com/mcp`
(ad-supported: one disclosed sponsored card per result, fan-out capped at 15, and clients that
cannot render the sponsored card may be capped further.) Come back here when the ads, the
15-search cap, or those client restrictions get in your way.

---

## Cursor Marketplace

Once listed, install directly from the [Cursor Marketplace](https://cursor.com/marketplace). Until
then, add the remote MCP server manually:

```bash
# In Cursor, add via Settings → MCP Servers → Add Server
# URL: https://google-flights-mcp.flightpowers.com/mcp
# Header: x-rapidapi-key: YOUR_RAPIDAPI_KEY
```

Or use the `mcp.json` at the root of this repo, which references `${RAPIDAPI_KEY}` as a plugin
variable.

## What your agent gets

Two tools that answer a *fare question*, not a *date lookup*.

- **Ask open-ended questions.** "Cheapest one-way to Sri Lanka anywhere in October", "5 to 7
  nights in Rome sometime in May, from Tel Aviv or Larnaca" — each is **one** tool call. Both
  tools take a departure date **range**, a **list** of destination airports, and (round-trip) a
  `nights` value instead of a fixed return date, and expand them internally.
- **Say whether a price is actually good.** Every result carries Google's own historical range
  for that route and period — `price_insights_low`, `price_insights_high`, and a
  `price_range_in_relation_to_other_periods` verdict of `low` / `typical` / `high`. That is what
  lets an agent answer "$209 is typical here, don't rush" instead of just quoting a number.
- **Book, not just browse.** Every result includes a `buy_link` to Google Flights.
- **Know what it spent.** Every response carries `api_usage` — requests used by this call, and
  what is left on the caller's plan. See [Spend reporting](#spend-reporting-api_usage).
- **Know what it searched.** Every response carries `search_coverage`, so the model can say
  honestly which dates and destinations the answer is based on.

Results are live fares. **They go stale within minutes — never cache a fare or reuse an earlier
result; search again and state when the data was fetched.**

## Get a key (free tier available)

The server holds no upstream credential of its own. Every search is billed to *your* RapidAPI
subscription, which is why the key travels with the request.

1. Subscribe to the Google Flights Live API:
   **https://rapidapi.com/mtnrabi/api/google-flights-live-api**
2. Copy your `x-rapidapi-key`.
3. Pass it to the server in any one of the three ways below.

If a key is missing, the tools do not fail silently and do not spend anything — they return
`needs_api_key: true` with the signup URL and these instructions, phrased for the model to read
back to you.

## Three ways to pass your key

| Way | How | When to use it |
|---|---|---|
| **Header** (preferred) | `--header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"` | Anything that lets you set headers. Keys stay out of URLs, and therefore out of proxy and access logs. |
| **Query parameter** | `https://google-flights-mcp.flightpowers.com/mcp?rapidapi_key=YOUR_RAPIDAPI_KEY` | Hosts that only let you paste a URL — claude.ai's custom-connector dialog is the case that matters. |
| **Client API-key field** | Paste the key into the client's own "API key" box | Hosts that send `authorization: Bearer <key>` or `x-api-key`. Smithery's saved-config form (`config.rapidApiKey=`) is also accepted. |

First non-empty source wins, in that order. The key is never logged, never echoed into an error
message, and never returned in a tool response.

## Gemini CLI

Install via the Gemini extensions CLI:

```bash
gemini extensions install https://github.com/mtnrabi/google-flights-mcp
```

The installer will prompt for your RapidAPI key. Subscribe at
https://rapidapi.com/mtnrabi/api/google-flights-live-api (free tier available) and copy your
`x-rapidapi-key`.

## Tools

| Tool | What it does |
|---|---|
| `search_oneway_flights` | Real-time one-way fares. Input: origin IATA, destination IATA **or a list**, and either one departure date or a date range. Returns price, airline, duration, stops, `buy_link`, and Google's historical price range so you can judge the fare. Use for any one-way question, including open-ended ones — one call with a range, never one call per date. |
| `search_roundtrip_flights` | Real-time round-trip fares priced as **paired legs**, not two one-ways. Input: origin, destination(s), a departure date or range, and either a `return_date` or a trip length in `nights` (a number or a list like `[5,6,7]`). Returns total price, per-leg airline/stops/duration, and one `buy_link` for the trip. |

### `search_oneway_flights`

```python
search_oneway_flights(
    from_airport: str,                     # origin IATA, e.g. "TLV"
    to_airport: str | list[str],           # destination IATA, or a list to compare
    departure_date: str | None = None,     # "YYYY-MM-DD"
    departure_date_from: str | None = None,# first date of a range
    departure_date_to: str | None = None,  # last date of a range
    max_stops: int | None = None,          # 0 = non-stop only
    airline_codes: list[str] | None = None,
    exclude_airline_codes: list[str] | None = None,
    departure_time_min: int | None = None, # hour, 0-23
    departure_time_max: int | None = None,
    arrival_time_min: int | None = None,
    arrival_time_max: int | None = None,
    currency: str = "usd",
    max_price: int | None = None,
    seat_type: int | None = None,          # 1 economy, 2 premium economy, 3 business, 4 first
    passengers: list[int] | None = None,   # [adults, children, infants]
    sort_by: str = "best",                 # "best" | "price" | "duration"
    limit: int = 10,                       # results returned after merge + sort
    max_searches: int | None = None,       # cap the billed requests this call may make
    use_fallback: bool = False,            # slower, fewer empty results on hard routes
)
```

### `search_roundtrip_flights`

```python
search_roundtrip_flights(
    from_airport: str,
    to_airport: str | list[str],
    departure_date: str | None = None,
    departure_date_from: str | None = None,
    departure_date_to: str | None = None,
    return_date: str | None = None,        # use this OR nights, not both
    nights: int | list[int] | None = None, # e.g. 7, or [5, 6, 7]
    max_departure_stops: int | None = None,
    max_return_stops: int | None = None,
    departure_airline_codes: list[str] | None = None,
    return_airline_codes: list[str] | None = None,
    currency: str = "usd",
    max_price: int | None = None,
    seat_type: int | None = None,
    passengers: list[int] | None = None,
    sort_by: str = "best",
    limit: int = 10,
    max_searches: int | None = None,
    use_fallback: bool = False,
)
```

`sort_by` is applied by this server across the merged result set from every search it ran, so it
is predictable regardless of how many combinations were expanded.

## A worked example

> **User:** "I'm in Tel Aviv. Cheapest week-long trip to Rome or Athens, leaving any day in the
> first half of May."

One call:

```json
{
  "name": "search_roundtrip_flights",
  "arguments": {
    "from_airport": "TLV",
    "to_airport": ["FCO", "ATH"],
    "departure_date_from": "2026-05-01",
    "departure_date_to": "2026-05-15",
    "nights": 7,
    "sort_by": "price",
    "limit": 5
  }
}
```

That expands to 15 dates × 2 destinations = 30 combinations, which is exactly the per-call cap.
The response shape (field names are real; **the values below are illustrative, not a quote** —
run the call to get live fares):

```json
{
  "results": [
    {
      "from_airport": "Tel Aviv (TLV)",
      "to_airport": "Rome (FCO)",
      "departure_date": "2026-05-05",
      "return_date": "2026-05-12",
      "total_price": "$XXX",
      "total_price_as_number": 0,
      "total_duration_seconds": 0,
      "total_stops": 0,
      "price_range_in_relation_to_other_periods": "low",
      "price_insights_low": 0,
      "price_insights_high": 0,
      "departure_flight_airline": "...",
      "departure_flight_departure_description": "...",
      "departure_flight_arrival_description": "...",
      "departure_flight_duration": "...",
      "departure_flight_stops": 0,
      "departure_stops_info": [],
      "return_flight_airline": "...",
      "return_flight_departure_description": "...",
      "return_flight_arrival_description": "...",
      "return_flight_duration": "...",
      "return_flight_stops": 0,
      "return_stops_info": [],
      "buy_link": "https://www.google.com/travel/flights?tfs=..."
    }
  ],
  "result_count": 5,
  "search_coverage": {
    "requested_combinations": 30,
    "searched_combinations": 30,
    "truncated": false,
    "max_searches_per_request": 30,
    "departure_dates_searched": ["2026-05-01", "..."],
    "destinations_searched": ["ATH", "FCO"]
  },
  "api_usage": {
    "requests_used_by_this_call": 30,
    "plan_requests_remaining": 0,
    "plan_requests_limit": 0,
    "note": "This search used 30 of your RapidAPI plan's requests; ... remain in the current period. Each date and destination combination is one billed request."
  }
}
```

Other response shapes to expect, all of them normal:

- **No flights on those dates.** `results: []` with a `message` — Google Flights genuinely
  returns nothing for some route/date combinations. Not an error. Try nearby dates, a nearby
  airport, or `use_fallback: true`.
- **Some searches failed.** A `partial` field says how many of the executed searches failed, and
  the results cover the rest.
- **Range too wide.** `search_coverage.truncated: true` plus a `note`. The range is sampled
  **evenly across the whole window** (first and last kept), not cut short — so the sample is
  representative, not the first N days. Raise `max_searches` or narrow the range for fuller
  coverage.
- **No key / rejected key.** `needs_api_key: true`, zero spend, with the fix. A valid RapidAPI
  key that is not subscribed to *this* API is the most common cause.
- **Plan exhausted.** `quota_exhausted: true` with `api_usage`, plus a reminder that narrowing
  the range makes remaining quota go further.

## Spend reporting (`api_usage`)

The money is yours, so the meter is visible. Every successful response carries:

| Field | Meaning |
|---|---|
| `requests_used_by_this_call` | Billed upstream requests this one tool call consumed. |
| `plan_requests_remaining` | What is left on your RapidAPI plan this period. |
| `plan_requests_limit` | Your plan's limit for the period. |
| `note` | The same thing in a sentence, so the model can relay it to you before you ask. |

`plan_requests_remaining` and `plan_requests_limit` come from the upstream response and are
omitted when upstream does not report them; the `note` adapts. The rule the model should state
out loud: **one date × one destination = one billed request.**

Cost control knobs, in order of bluntness: `max_searches` per call (lower it to spend less on a
wide question), a narrower date range, a shorter destination list.

## One call vs thirty

The underlying REST API takes exactly one `(origin, destination, date)` tuple per call. Against a
one-date-per-call passthrough, "cheapest to Sri Lanka anywhere in October" is 31 separate tool
calls — 31 round trips through the model, 31 chances to lose the thread, and a bill the user only
discovers afterwards.

Here it is **one** tool call. The fan-out happens server-side, concurrently, capped, evenly
sampled, deduplicated on `buy_link`, merged, sorted by your `sort_by`, and reported honestly in
`search_coverage` and `api_usage`.

| | This server (paid) | Free server |
|---|---|---|
| Fan-out per call | 30 (hard max 60; raise or lower per call with `max_searches`) | 15 |
| Ads | none | one disclosed sponsored card per result |
| Key | your own RapidAPI key | none needed |
| Spend reporting | `api_usage` in every response | n/a |
| Directory-listable | yes | no |

**This server carries no ads at all** — not by taste but by constraint: Anthropic's connector
directory policy and OpenAI's app guidelines both prohibit advertising and sponsored content in
tool results, so an ad-carrying server can never be listed there and this one can.

## Local development

```bash
git clone <this repo> && cd mcp_server_paid
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp example.env .env          # fill it in; leave RAPIDAPI_KEY empty
set -a && . .env && set +a
.venv/bin/python -m src      # streamable HTTP on http://localhost:8000/mcp
```
<!-- untested — developer verify: clone/venv/run steps not executed in this environment -->

Point a client at the local process the same way:

```bash
claude mcp add --transport http google-flights-local http://localhost:8000/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```
<!-- untested — developer verify -->

Tests (124 passing, verified):

```bash
.venv/bin/python -m pytest -q
```

Configuration lives in `example.env`; every variable is documented there. The ones that matter:

| Variable | Default | Why it matters |
|---|---|---|
| `MAX_SEARCHES_PER_TOOL_CALL` | `30` | Per-call fan-out cap. Clamped to a hard maximum of 60. |
| `MAX_CONCURRENT_SEARCHES` | `10` | Concurrency of the fan-out. |
| `MAX_HTTP_CONNECTIONS` | `60` | Connection-pool ceiling; serverless instances share a file-descriptor pool. |
| `REQUEST_TIMEOUT_SECONDS` | `105` | Matches the upstream ceiling, so this side never times out first. |
| `DEFAULT_RESULT_LIMIT` | `10` | Results requested per individual upstream search. |
| `SIGNUP_URL` | RapidAPI listing | Quoted back to users who arrive without a key. |
| `MCP_PUBLIC_URL` | `http://localhost:8000/mcp` | Reported by `/health`. |
| `RAPIDAPI_KEY` | *(empty)* | **Leave empty in production.** If set, every keyless caller is served on — and billed to — that subscription. The server logs a warning at startup and `/health` reports `server_side_key_configured`. |
| `METRICS_TOKEN` | *(empty)* | When set, `/metrics` requires an `x-metrics-token` header. |
| `LOG_PATH` | *(empty)* | Empty disables the file sink; stdout `MCP_CALL` lines remain the record. Correct on serverless. |

Operational routes: `GET /health` (public, unauthenticated — registries poll it),
`GET /metrics`, `GET /metrics/calls?hours=24`.

Deployment target is Vercel via `api/index.py` (FastAPI wrapper handing FastMCP its lifespan,
`stateless_http=True`). The canonical MCP path is `/mcp`, **no trailing slash**.

Never commit a real key. `example.env` ships with placeholders; keep it that way.

## Non-affiliation

This is an independent API that returns publicly available flight pricing. It is **not affiliated
with, endorsed by, or sponsored by Google**. "Google Flights" is used only to describe the public
data source. Fares are supplied by the upstream provider, change constantly, and are not
guaranteed — always confirm the price on the airline or booking site before purchase.
