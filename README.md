# Google Flights MCP: real-time fares your agent can search across a whole date range, ad-free

**Sign in, no key to paste:**

```bash
claude mcp add --transport http google-flights https://flights.flightpowers.com/mcp/oauth
```

Works in clients that support MCP authorization: a Sign in button appears, you sign in with
Google, and you paste your RapidAPI key once on the `/connect` page. Nothing goes in your client
config.

**Or bring your own RapidAPI key:**

```bash
claude mcp add --transport http google-flights https://google-flights-mcp.flightpowers.com/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```

Hosted. Nothing to clone, nothing to build. Listed in the official MCP Registry as
`com.flightpowers/google-flights-mcp`. Health check:
[`/health`](https://google-flights-mcp.flightpowers.com/health).

**Need a key?** Subscribe to the Google Flights Live API on RapidAPI, free tier available,
and copy your `x-rapidapi-key`: **https://rapidapi.com/mtnrabi/api/google-flights-live-api**

**No key yet? Start with the free server, same search, no signup:**
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
  nights in Rome sometime in May, from Tel Aviv or Larnaca": each is **one** tool call. Both
  tools take a departure date **range**, a **list** of destination airports, and (round-trip) a
  `nights` value instead of a fixed return date, and expand them internally.
- **Say whether a price is actually good.** Every result carries Google's own historical range
  for that route and period: `price_insights_low`, `price_insights_high`, and a
  `price_range_in_relation_to_other_periods` verdict of `low` / `typical` / `high`. That is what
  lets an agent answer "$209 is typical here, don't rush" instead of just quoting a number.
- **Book, not just browse.** Every result includes a `buy_link` to Google Flights.
- **Know what it spent.** Every response carries `api_usage`: requests used by this call, and
  what is left on the caller's plan. See [Spend reporting](#spend-reporting-api_usage).
- **Know what it searched.** Every response carries `search_coverage`, so the model can say
  honestly which dates and destinations the answer is based on.

Results are live fares. **They go stale within minutes: never cache a fare or reuse an earlier
result; search again and state when the data was fetched.**

## Get a key (free tier available)

The server holds no upstream credential of its own. Every search is billed to *your* RapidAPI
subscription, which is why the key travels with the request.

1. Subscribe to the Google Flights Live API:
   **https://rapidapi.com/mtnrabi/api/google-flights-live-api**
2. Copy your `x-rapidapi-key`.
3. Pass it to the server in any one of the three ways below.

If a key is missing, the tools do not fail silently and do not spend anything. They return
`needs_api_key: true` with the signup URL and these instructions, phrased for the model to read
back to you.

## Three ways to pass your key

| Way | How | When to use it |
|---|---|---|
| **Header** (preferred) | `--header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"` | Anything that lets you set headers. Keys stay out of URLs, and therefore out of proxy and access logs. |
| **Query parameter** | `https://google-flights-mcp.flightpowers.com/mcp?rapidapi_key=YOUR_RAPIDAPI_KEY` | Hosts that only let you paste a URL: claude.ai's custom-connector dialog is the case that matters. |
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

## A fourth way: sign in once at `/connect`

This is the page the sign-in URL at the top of this README sends you to. A client that speaks MCP
authorization walks you through it on its own; the steps below are the same thing done by hand.

Where a deployment has it enabled (check `connect_enabled` on `/health`), there is a page at
`/connect` that replaces all of the above with a sign-in:

1. Open **https://google-flights-mcp.flightpowers.com/connect** (hotels:
   **https://hotels.flightpowers.com/connect**) and sign in with Google.
2. Paste your RapidAPI key once, into a form, over TLS.
3. Copy the connect URL it gives you back, `…/mcp?fp_token=fpk_…`, and use that as the server
   URL in your MCP client. Clients that let you set headers can send the same token as
   `Authorization: Bearer fpk_…` instead.

What that buys you: your RapidAPI key is not in your client config, not in a URL, and not in
whatever logs that URL passes through. What it costs: the server stores your key, encrypted, and
knows your Google account id and email address. `Disconnect` on the same page deletes the record
and kills every connect token for your account, immediately. The full description is
[section 2a of the privacy policy](https://google-flights-mcp.flightpowers.com/privacy).

Some details worth knowing:

- **Saving runs one check.** The key is validated against the listing before it is stored, so a
  typo fails on the page rather than in your client an hour later. That check costs **at most one
  request** from your own plan: on the free BASIC plan (10 a month), one of ten. A key that
  RapidAPI rejects at the gateway costs nothing.
- **A key on the request always wins.** If you send an `x-rapidapi-key` header (or any of the
  other channels above) *and* carry a connect token, the request's own key is used. Nothing you
  already have set up changes behaviour because you signed in.
- **The token is not your key** and cannot be turned back into it. It is valid for 90 days, and
  it stops resolving the moment you disconnect. A call carrying a token whose key has been
  disconnected gets a `needs_api_key` reply telling you to reconnect. It never falls back to
  somebody else's subscription and never spends anything.
- **BASIC is free.** [Google Flights Live API](https://rapidapi.com/mtnrabi/api/google-flights-live-api)
  · [Booking Live API](https://rapidapi.com/mtnrabi/api/booking-live-api). One RapidAPI key covers
  whichever of the two you have subscribed to; you connect it once.

### Running `/connect` on your own deployment

Off unless **all four** of these are set. A half-configured deployment registers none of the
routes and serves keyed callers exactly as before; `/health` reports `connect_enabled` so that is
visible rather than guessed.

| Variable | What it is |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Google Cloud Console → Credentials → OAuth client ID, type **Web application**. Ends `.apps.googleusercontent.com`. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | The same client's secret (`GOCSPX-…`). |
| `MCP_KEY_MASTER` | 32 bytes, base64: `openssl rand -base64 32`. Encrypts stored keys (AES-256-GCM) and derives the cookie and token signing keys. |
| `DATABASE_URL` | Neon Postgres, **pooled** endpoint (`…-pooler…`). Schema: `migrations/001_mcp_user_keys.sql`. |

Optional: `MCP_CONNECT_VALIDATE=0` stores a pasted key without checking it first.

**Authorised redirect URIs to register on the Google client**, one per product origin, exactly:

```
https://google-flights-mcp.flightpowers.com/connect/callback
https://hotels.flightpowers.com/connect/callback
```

`flights.flightpowers.com` needs **no** entry. `/connect` and `/connect/start` bounce an alias to
the canonical origin before the sign-in starts, because cookies are per-host and Google compares
`redirect_uri` literally: an alias that started its own sign-in would come back to a host with no
state cookie and fail with a message that reads like a Google misconfiguration.

Also on the OAuth consent screen: scopes `openid` and `.../auth/userinfo.email`, and nothing else.

**Rotating `MCP_KEY_MASTER` logs everybody out and invalidates every stored key.** That is
deliberate: after a rotation nothing is left holding a token that resolves to a key nobody can
read. Users see "connect again", not a failed search. `key_version` on the table is there so a
staged rotation is possible later without a flag day.

### Verifying the flow end to end

Migration first, once per database:

```bash
psql "$DATABASE_URL" -f migrations/001_mcp_user_keys.sql
```

Then, after deploying:

```bash
# 1. The feature is actually on.
curl -s https://google-flights-mcp.flightpowers.com/health | grep connect_enabled

# 2. The page renders for an anonymous visitor.
curl -sI https://google-flights-mcp.flightpowers.com/connect        # 200
curl -sI https://google-flights-mcp.flightpowers.com/connect/start  # 302 to accounts.google.com

# 3. Sign in in a browser, paste a key, copy the connect URL.

# 4. MCP Inspector against that URL -- list the tools, then run one real search.
npx @modelcontextprotocol/inspector
#   Transport: Streamable HTTP
#   URL: https://google-flights-mcp.flightpowers.com/mcp?fp_token=fpk_...

# 5. Claude Code, the same URL.
claude mcp add --transport http flightpowers \
  "https://google-flights-mcp.flightpowers.com/mcp?fp_token=fpk_..."
claude mcp list          # shows it connected
#   then, in a session: ask for a fare and check the result is real

# 6. Cursor: Settings -> MCP -> Add, same URL. Or in ~/.cursor/mcp.json:
#   { "mcpServers": { "flightpowers": {
#       "url": "https://google-flights-mcp.flightpowers.com/mcp?fp_token=fpk_..." } } }

# 7. Hotels, the other hostname, with the same token.
#   https://hotels.flightpowers.com/mcp?fp_token=fpk_...

# 8. Press Disconnect on /connect, then re-run step 4. The tool must answer
#    needs_api_key with a "connect again" message -- not a search, and not a
#    generic "get a key" reply.
```

A `tools/list` that succeeds proves nothing about any of this: a token is only consulted when a
tool actually runs. Step 4 has to be a **real search**.

## A fifth way: sign in from inside your MCP client (`/mcp/oauth`)

`/connect` works, but no MCP client will ever *start* a sign-in on its own, because nothing on
the wire tells it one is available. A client only begins an OAuth flow when a request comes back `401` with
a `WWW-Authenticate: Bearer resource_metadata=…` header, and `/mcp` must never do that: every
paying caller today authenticates with a RapidAPI key and no bearer token, so challenging them
would be an outage rather than a feature.

So there is a **second endpoint** that always challenges:

| URL | Who it is for |
|---|---|
| `https://google-flights-mcp.flightpowers.com/mcp` | Unchanged, forever. A RapidAPI key, an `fpk_` connect token, a Smithery config blob, or an anonymous `tools/list`. No 401, ever. |
| `https://google-flights-mcp.flightpowers.com/mcp/oauth` | Bearer-only. Paste this one and your client shows a **Sign in** button, walks you through Google, and manages the token itself. |
| `https://hotels.flightpowers.com/mcp/oauth` | The same, for hotels. |

Same tools, same product-per-hostname routing, same everything else. The OAuth endpoint is the
identical tool registry behind a token check, not a second copy of the server.

**What it is like to use.** Paste the `/mcp/oauth` URL into your client. It registers itself,
opens a browser, you sign in with Google, and you approve *that client by name* on a page that
says exactly what it will be able to do: run searches billed to your own RapidAPI plan, nothing
else. The client never sees your RapidAPI key. If you have not connected one yet, the approval
still works and the first search comes back telling you to paste a key at `/connect`, with the
URL.

**What you can revoke, and how.** Press **Disconnect** on `/connect`: the stored key is deleted
*and* every OAuth token for that Google account is dropped in the same action. A client that was
connected stops working immediately. Individually, a client can call `/oauth/revoke` (RFC 7009).

### Client setup

```bash
# Claude Code
claude mcp add --transport http flightpowers \
  "https://google-flights-mcp.flightpowers.com/mcp/oauth"
claude mcp list            # shows "needs authentication" until you sign in
/mcp                       # in a session: pick the server, follow the sign-in

# Cursor -- Settings -> MCP -> Add, URL above. Or ~/.cursor/mcp.json:
#   { "mcpServers": { "flightpowers": {
#       "url": "https://google-flights-mcp.flightpowers.com/mcp/oauth" } } }
# Cursor discovers the 401, registers itself and opens the browser.

# ChatGPT -- Settings -> Connectors -> Create. It asks for:
#   MCP server URL:  https://google-flights-mcp.flightpowers.com/mcp/oauth
#   Authentication:  OAuth
# Leave client id and secret EMPTY: this server supports dynamic client
# registration, so ChatGPT registers itself. Nothing else has to be filled in.

# MCP Inspector -- the quickest way to watch the whole handshake.
npx @modelcontextprotocol/inspector
#   Transport: Streamable HTTP
#   URL: https://google-flights-mcp.flightpowers.com/mcp/oauth
#   Auth: OAuth 2.0  ->  "Guided OAuth Flow" walks metadata -> DCR ->
#   authorize -> token, and shows each response. Then run ONE real search.
```

### The protocol surface

| Route | Spec |
|---|---|
| `GET /.well-known/oauth-protected-resource` and `…/mcp/oauth` | RFC 9728 |
| `GET /.well-known/oauth-authorization-server` and `…/mcp/oauth` | RFC 8414 |
| `POST /oauth/register` | RFC 7591, dynamic client registration, open |
| `GET /connect/authorize` · `POST /connect/authorize` | RFC 6749 §4.1, **PKCE S256 required** |
| `POST /oauth/token` | `authorization_code` and `refresh_token` |
| `POST /oauth/revoke` | RFC 7009 |

The authorization endpoint is under `/connect` on purpose: the sign-in session cookie is scoped
`Path=/connect` so it can never be attached to a `/mcp` request, and putting authorize anywhere
else would mean either widening that cookie or making the user sign in twice.

Codes live 10 minutes and are single-use (`DELETE … RETURNING`, so two concurrent exchanges race
on one row and exactly one wins). Access tokens live 1 hour, refresh tokens 30 days with rotation.
Everything is opaque and stored as a SHA-256 hash, so a dump of the database contains nothing
that can be replayed. Tokens are **not** JWTs, deliberately: a signed token stays valid until it
expires whatever we decide afterwards, and Disconnect has to mean disconnect.

An access token is checked against the resource it was approved for before it is accepted, not
only when it is issued. Both products are the same deployment, the same database and the same
stored RapidAPI key per user, so without that check a token approved on the flights consent
page, which says "search live flight fares" and nothing else, would be accepted on the hotels
hostname and spend the user's hotels plan. The check is strict about the host and forgiving about
the path, because clients in the wild send the origin, `/mcp` and `/mcp/oauth` for the same
server. `tests/test_oauth.py::TestATokenIsBoundToTheResourceItWasApprovedFor` pins both halves.

### Running it on your own deployment

**No new environment variable.** It comes on wherever `/connect` is configured, because it reuses
that Google sign-in and that key store. It needs one more table in the same database:

```bash
psql "$DATABASE_URL" -f migrations/002_mcp_oauth.sql
```

`/health` then reports `oauth_enabled: true` and `oauth_mcp_endpoint`; that URL is what goes in
a directory listing. `MCP_OAUTH=off` disables it while leaving `/connect` running; that is the
rollback that needs no code change.

Nothing has to change on the Google OAuth client. The redirect URI is still
`…/connect/callback`, because the MCP client's OAuth flow ends at *our* authorize page, and only
that page talks to Google.

### Verifying it end to end

```bash
BASE=https://google-flights-mcp.flightpowers.com

# 1. On, and advertising itself.
curl -s $BASE/health | python3 -m json.tool | grep oauth_

# 2. The challenge. This is the whole feature in one response.
curl -si $BASE/mcp/oauth | head -20
#   HTTP/2 401
#   www-authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp/oauth"

# 3. Discovery, at both the scoped and the bare path.
curl -s $BASE/.well-known/oauth-protected-resource/mcp/oauth | python3 -m json.tool
curl -s $BASE/.well-known/oauth-authorization-server | python3 -m json.tool

# 4. Dynamic registration answers.
curl -s -X POST $BASE/oauth/register -H 'content-type: application/json' \
  -d '{"client_name":"probe","redirect_uris":["http://127.0.0.1:9999/cb"],
       "token_endpoint_auth_method":"none"}' | python3 -m json.tool

# 5. The real test: MCP Inspector, Guided OAuth Flow, then ONE real search.
#    A tools/list proves nothing -- the token is only consulted when a tool runs.

# 6. Hotels, the other hostname, same walk: https://hotels.flightpowers.com/mcp/oauth

# 7. /mcp is untouched. This must still work, with no 401 anywhere:
curl -s -X POST $BASE/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H "x-rapidapi-key: $REAL_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'

# 8. Disconnect on /connect, then re-run step 5's search: it must answer
#    needs_api_key, and the client must be logged out.
```

## Tools

| Tool | What it does |
|---|---|
| `search_oneway_flights` | Real-time one-way fares. Input: origin IATA, destination IATA **or a list**, and either one departure date or a date range. Returns price, airline, duration, stops, `buy_link`, and Google's historical price range so you can judge the fare. Use for any one-way question, including open-ended ones, one call with a range, never one call per date. |
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
    use_fallback: bool | None = None,      # leave unset: accepted upstream, currently inert
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
    use_fallback: bool | None = None,
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
The response shape (field names are real; **the values below are illustrative, not a quote** , 
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

- **No flights on those dates.** `results: []` with a `message`, Google Flights genuinely
  returns nothing for some route/date combinations. Not an error. Try nearby dates or a
  nearby airport. `use_fallback` will not change this and is left unset by default: the
  backend accepts the field, but the second flight-data source it selects is gated behind
  `USE_FALLBACK_FLI` (`fallback_available()`), which is not switched on for this API, so
  none of its three values has any observable effect on a search today. The automatic
  retries the backend does on an unreadable page are unconditional and are not affected
  by it.
- **Some searches failed.** A `partial` field says how many of the executed searches failed, and
  the results cover the rest.
- **Range too wide.** `search_coverage.truncated: true` plus a `note`. The range is sampled
  **evenly across the whole window** (first and last kept), not cut short, so the sample is
  representative, not the first N days. Raise `max_searches` or narrow the range for fuller
  coverage.
- **No key / rejected key.** `needs_api_key: true`, zero spend, with the fix. A valid RapidAPI
  key that is not subscribed to *this* API is the most common cause.
- **Plan exhausted.** `quota_exhausted: true` with `api_usage`, plus a reminder that narrowing
  the range makes remaining quota go further.

## Structured output (`outputSchema`, `structuredContent`, `isError`)

Every tool declares an `outputSchema`, and every result carries the payload
twice: once as `structuredContent`, once as the serialized JSON in a text
content block. The MCP spec asks for the duplicate --

> For backwards compatibility, a tool that returns structured content SHOULD
> also return the serialized JSON in a TextContent block.

-- and it is load-bearing here rather than ceremonial, because clients that
predate structured output read the text block and nothing else. (Verified
against spec revision **2026-07-28**; structured output arrived in
2025-06-18.)

The schemas are deliberately `additionalProperties: true` with only `results`
required. The spec puts the obligation on the server -- "Servers MUST provide
structured results that conform to this schema" -- and these tools have
several legitimate exits that carry different keys -- a zero-result answer, the keyless
`needs_api_key` reply and the `quota_exhausted` reply. A tighter
schema would look better and would make the server non-conformant on a path
it ships on purpose.

### `search_status`, and why `degraded` is an error

Flight results carry `search_status`, mirroring the backend's own
`X-Search-Status` vocabulary:

| value | meaning |
|---|---|
| `ok` | every combination searched returned results |
| `empty` | the search completed; Google genuinely has no itineraries. A real answer |
| `partial` | some combinations returned results, some failed. The list is incomplete |
| `degraded` | every combination failed. The search did not happen; an empty list means nothing |

A `degraded` result is **also flagged `isError: true`**. It is the only one
that is. The spec classifies "API failures" as tool execution errors and says
clients "SHOULD provide tool execution errors to language models to enable
self-correction", while nothing in the spec obliges a host to show
`structuredContent` to the model at all. A failure carried only by a field
inside the payload is therefore a failure the model may never see, which was
the whole problem `search_status` was added to solve.

The payload still rides along with the error -- `structuredContent` and the
text block are both present, so nothing is lost. `api_usage` in particular: a degraded
search still spent the caller's own RapidAPI requests, and hiding that would
hide a charge they have to pay. `empty` and
`partial` are not errors: one is a true negative and the other carries
results a caller can use.

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
calls, 31 round trips through the model, 31 chances to lose the thread, and a bill the user only
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

**This server carries no ads at all**, not by taste but by constraint: Anthropic's connector
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
<!-- untested, developer verify: clone/venv/run steps not executed in this environment -->

Point a client at the local process the same way:

```bash
claude mcp add --transport http google-flights-local http://localhost:8000/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```
<!-- untested, developer verify -->

Tests (620 passing, verified):

```bash
.venv/bin/python -m pytest -q
```

Configuration lives in `example.env`; every variable is documented there. The ones that matter:

| Variable | Default | Why it matters |
|---|---|---|
| `MAX_SEARCHES_PER_TOOL_CALL` | `30` | Per-call fan-out cap. Clamped to a hard maximum of 60. |
| `MAX_CONCURRENT_SEARCHES` | `10` | Concurrency of the fan-out. |
| `MAX_HTTP_CONNECTIONS` | `60` | Connection-pool ceiling; serverless instances share a file-descriptor pool. |
| `REQUEST_TIMEOUT_SECONDS` | `75` | The upstream function's `Timeout` (60) plus a 15s edge-relay margin, so this side never gives up on an answer that is still coming. |
| `DEFAULT_RESULT_LIMIT` | `10` | Results requested per individual upstream search. |
| `MCP_PRODUCTS` | `both` | Which product this deployment serves: `flights`, `hotels` or `both`. Selects the tool set, the server instructions, the service name, the policy pages and the RapidAPI listing a keyless or unsubscribed caller is sent to. A hotels deployment left on the default introduces itself as a flights server. |
| `SIGNUP_URL` | listing matching `MCP_PRODUCTS` | Quoted back to users who arrive without a key. On `both`, the hotel tools quote the Booking listing regardless, one URL cannot be the Subscribe button for two APIs. |
| `MCP_PRODUCTS_BY_HOST` | `default` | Which product each hostname serves, so one deployment can carry both paid domains and each listing still gets exactly its own tool set. `default` is the built-in map of flightpowers.com aliases; `off` disables host routing entirely (the no-code rollback); or an explicit `host=product,…` map. An unmapped hostname falls back to `MCP_PRODUCTS`. |
| `MCP_PUBLIC_URL` | `http://localhost:8000/mcp` | Reported by `/health` and the origin of every policy-page link. `MCP_PUBLIC_URL_FLIGHTS` / `MCP_PUBLIC_URL_HOTELS` override it per product on a deployment serving both, without them the hotels hostname would advertise the flights one. `SIGNUP_URL_FLIGHTS` / `SIGNUP_URL_HOTELS` work the same way. |
| `RAPIDAPI_KEY` | *(empty)* | **Leave empty in production.** If set, every keyless caller is served on, and billed to, that subscription. The server logs a warning at startup and `/health` reports `server_side_key_configured`. |
| `METRICS_TOKEN` | *(empty)* | When set, `/metrics` requires an `x-metrics-token` header. |
| `LOG_PATH` | *(empty)* | Empty disables the file sink; stdout `MCP_CALL` lines remain the record. Correct on serverless. |

Operational routes: `GET /health` (public, unauthenticated, registries poll it),
`GET /metrics`, `GET /metrics/calls?hours=24`.

Deployment target is Vercel via `api/index.py` (FastAPI wrapper handing FastMCP its lifespan,
`stateless_http=True`). The canonical MCP path is `/mcp`, **no trailing slash**.

Never commit a real key. `example.env` ships with placeholders; keep it that way.

## Non-affiliation

This is an independent API that returns publicly available flight pricing. It is **not affiliated
with, endorsed by, or sponsored by Google**. "Google Flights" is used only to describe the public
data source. Fares are supplied by the upstream provider, change constantly, and are not
guaranteed, always confirm the price on the airline or booking site before purchase.

## Tools

| Tool | What it does |
|---|---|
| `search_oneway_flights` | Real-time one-way fares. Input: origin IATA, destination IATA **or a list**, and either one departure date or a date range. Returns price, airline, duration, stops, `buy_link`, and Google's historical price range so you can judge the fare. Use for any one-way question, including open-ended ones: one call with a range, never one call per date. |
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
    use_fallback: bool | None = None,      # leave unset: accepted upstream, currently inert
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
    use_fallback: bool | None = None,
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
The response shape (field names are real; **the values below are illustrative, not a quote**,
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

- **No flights on those dates.** `results: []` with a `message`: Google Flights genuinely
  returns nothing for some route/date combinations. Not an error. Try nearby dates or a
  nearby airport. `use_fallback` will not change this and is left unset by default: the
  backend accepts the field, but the second flight-data source it selects is gated behind
  `USE_FALLBACK_FLI` (`fallback_available()`), which is not switched on for this API, so
  none of its three values has any observable effect on a search today. The automatic
  retries the backend does on an unreadable page are unconditional and are not affected
  by it.
- **Some searches failed.** A `partial` field says how many of the executed searches failed, and
  the results cover the rest.
- **Range too wide.** `search_coverage.truncated: true` plus a `note`. The range is sampled
  **evenly across the whole window** (first and last kept), not cut short, so the sample is
  representative, not the first N days. Raise `max_searches` or narrow the range for fuller
  coverage.
- **No key / rejected key.** `needs_api_key: true`, zero spend, with the fix. A valid RapidAPI
  key that is not subscribed to *this* API is the most common cause.
- **Plan exhausted.** `quota_exhausted: true` with `api_usage`, plus a reminder that narrowing
  the range makes remaining quota go further.

## Structured output (`outputSchema`, `structuredContent`, `isError`)

Every tool declares an `outputSchema`, and every result carries the payload
twice: once as `structuredContent`, once as the serialized JSON in a text
content block. The MCP spec asks for the duplicate --

> For backwards compatibility, a tool that returns structured content SHOULD
> also return the serialized JSON in a TextContent block.

-- and it is load-bearing here rather than ceremonial, because clients that
predate structured output read the text block and nothing else. (Verified
against spec revision **2026-07-28**; structured output arrived in
2025-06-18.)

The schemas are deliberately `additionalProperties: true` with only `results`
required. The spec puts the obligation on the server -- "Servers MUST provide
structured results that conform to this schema" -- and these tools have
several legitimate exits that carry different keys -- a zero-result answer, the keyless
`needs_api_key` reply and the `quota_exhausted` reply. A tighter
schema would look better and would make the server non-conformant on a path
it ships on purpose.

### `search_status`, and why `degraded` is an error

Flight results carry `search_status`, mirroring the backend's own
`X-Search-Status` vocabulary:

| value | meaning |
|---|---|
| `ok` | every combination searched returned results |
| `empty` | the search completed; Google genuinely has no itineraries. A real answer |
| `partial` | some combinations returned results, some failed. The list is incomplete |
| `degraded` | every combination failed. The search did not happen; an empty list means nothing |

A `degraded` result is **also flagged `isError: true`**. It is the only one
that is. The spec classifies "API failures" as tool execution errors and says
clients "SHOULD provide tool execution errors to language models to enable
self-correction", while nothing in the spec obliges a host to show
`structuredContent` to the model at all. A failure carried only by a field
inside the payload is therefore a failure the model may never see, which was
the whole problem `search_status` was added to solve.

The payload still rides along with the error -- `structuredContent` and the
text block are both present, so nothing is lost. `api_usage` in particular: a degraded
search still spent the caller's own RapidAPI requests, and hiding that would
hide a charge they have to pay. `empty` and
`partial` are not errors: one is a true negative and the other carries
results a caller can use.

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
calls: 31 round trips through the model, 31 chances to lose the thread, and a bill the user only
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

**This server carries no ads at all**, not by taste but by constraint: Anthropic's connector
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
<!-- untested, developer verify: clone/venv/run steps not executed in this environment -->

Point a client at the local process the same way:

```bash
claude mcp add --transport http google-flights-local http://localhost:8000/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```
<!-- untested, developer verify -->

Tests (620 passing, verified):

```bash
.venv/bin/python -m pytest -q
```

Configuration lives in `example.env`; every variable is documented there. The ones that matter:

| Variable | Default | Why it matters |
|---|---|---|
| `MAX_SEARCHES_PER_TOOL_CALL` | `30` | Per-call fan-out cap. Clamped to a hard maximum of 60. |
| `MAX_CONCURRENT_SEARCHES` | `10` | Concurrency of the fan-out. |
| `MAX_HTTP_CONNECTIONS` | `60` | Connection-pool ceiling; serverless instances share a file-descriptor pool. |
| `REQUEST_TIMEOUT_SECONDS` | `75` | The upstream function's `Timeout` (60) plus a 15s edge-relay margin, so this side never gives up on an answer that is still coming. |
| `DEFAULT_RESULT_LIMIT` | `10` | Results requested per individual upstream search. |
| `MCP_PRODUCTS` | `both` | Which product this deployment serves: `flights`, `hotels` or `both`. Selects the tool set, the server instructions, the service name, the policy pages and the RapidAPI listing a keyless or unsubscribed caller is sent to. A hotels deployment left on the default introduces itself as a flights server. |
| `SIGNUP_URL` | listing matching `MCP_PRODUCTS` | Quoted back to users who arrive without a key. On `both`, the hotel tools quote the Booking listing regardless: one URL cannot be the Subscribe button for two APIs. |
| `MCP_PRODUCTS_BY_HOST` | `default` | Which product each hostname serves, so one deployment can carry both paid domains and each listing still gets exactly its own tool set. `default` is the built-in map of flightpowers.com aliases; `off` disables host routing entirely (the no-code rollback); or an explicit `host=product,…` map. An unmapped hostname falls back to `MCP_PRODUCTS`. |
| `MCP_PUBLIC_URL` | `http://localhost:8000/mcp` | Reported by `/health` and the origin of every policy-page link. `MCP_PUBLIC_URL_FLIGHTS` / `MCP_PUBLIC_URL_HOTELS` override it per product on a deployment serving both. Without them the hotels hostname would advertise the flights one. `SIGNUP_URL_FLIGHTS` / `SIGNUP_URL_HOTELS` work the same way. |
| `RAPIDAPI_KEY` | *(empty)* | **Leave empty in production.** If set, every keyless caller is served on, and billed to, that subscription. The server logs a warning at startup and `/health` reports `server_side_key_configured`. |
| `METRICS_TOKEN` | *(empty)* | When set, `/metrics` requires an `x-metrics-token` header. |
| `LOG_PATH` | *(empty)* | Empty disables the file sink; stdout `MCP_CALL` lines remain the record. Correct on serverless. |

Operational routes: `GET /health` (public, unauthenticated: registries poll it),
`GET /metrics`, `GET /metrics/calls?hours=24`.

Deployment target is Vercel via `api/index.py` (FastAPI wrapper handing FastMCP its lifespan,
`stateless_http=True`). The canonical MCP path is `/mcp`, **no trailing slash**.

Never commit a real key. `example.env` ships with placeholders; keep it that way.

## Run it in a container

The hosted server needs nothing installed. This is for self-hosting, and it is what lets
[Glama](https://glama.ai/mcp/servers/mtnrabi/google-flights-mcp) run its build test and cut a
release.

```bash
docker build -t flightpowers-mcp .
docker run --rm -p 8000:8000 flightpowers-mcp
curl http://localhost:8000/health
```
<!-- untested in CI, no Docker daemon on the machine that wrote this; the exact file set the
     image copies (requirements.txt, src/, legal/) and the exact start command were installed
     into a clean venv on Python 3.12 and booted: /health 200, /privacy and /terms 200,
     MCP initialize + tools/list returned all four tools. -->

The container serves streamable HTTP on `${PORT}/mcp`, the same transport as the hosted
deployment, via `python -m src`. `api/index.py` is the Vercel wrapper and is not used here.

**No secret is baked into the image.** Every search is billed to the caller's own RapidAPI
subscription and their key travels with the request as `x-rapidapi-key`. `RAPIDAPI_KEY` is
optional and is a server-side *fallback*: when set, a caller who sends no key of their own is
served on, and billed to, that subscription. Leave it unset unless that is what you want;
`/health` reports `server_side_key_configured` either way.

```bash
# optional, local development only
docker run --rm -p 8000:8000 -e RAPIDAPI_KEY=your_key flightpowers-mcp
```

Every variable in the table above works as `-e NAME=value`. `HOST` defaults to `0.0.0.0` and
`PORT` to `8000` inside the image. The `HEALTHCHECK` polls `/health` on `$PORT`, so overriding
`PORT` keeps working.

## Non-affiliation

This is an independent API that returns publicly available flight pricing. It is **not affiliated
with, endorsed by, or sponsored by Google**. "Google Flights" is used only to describe the public
data source. Fares are supplied by the upstream provider, change constantly, and are not
guaranteed. Always confirm the price on the airline or booking site before purchase.
