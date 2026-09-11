# Google Flights MCP: real-time fares your agent can search across a whole date range, ad-free

**One URL, either way in:**

```bash
claude mcp add --transport http google-flights https://flights.flightpowers.com/mcp
```

In a client that supports MCP authorization, a **Sign in** button appears: you sign in with
Google and paste your RapidAPI key once on the `/connect` page, and nothing goes in your client
config. In a client that does not, bring the key yourself:

```bash
claude mcp add --transport http google-flights https://flights.flightpowers.com/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```

Same URL, both times. A request carrying a credential of any kind is served; a request carrying
nothing at all is answered `401` with the OAuth metadata, which is what makes the Sign in button
appear. `https://flights.flightpowers.com/mcp/oauth` is still live and demands the sign-in on the
first request, for clients whose auth mode is fixed when a server is added.

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

## A fourth way: sign in once at `/connect`

This is the page the sign-in URL at the top of this README sends you to. A client that speaks MCP
authorization walks you through it on its own; the steps below are the same thing done by hand.

Where a deployment has it enabled (check `connect_enabled` on `/health`), there is a page at
`/connect` that replaces all of the above with a sign-in:

1. Open **https://google-flights-mcp.flightpowers.com/connect** (hotels:
   **https://hotels.flightpowers.com/connect**) and sign in with Google.
2. Paste your RapidAPI key once, into a form, over TLS.
3. Press **Reveal the URL** and copy the connect URL, `…/mcp?fp_token=fpk_…`, and use that as
   the server URL in your MCP client. Clients that let you set headers can send the same token
   as `Authorization: Bearer fpk_…` instead. (It is hidden until you ask for it: that URL is a
   90-day bearer credential for your plan, and a page that prints one by default puts it in
   every screenshot and screen share.)

If your client signed you in itself -- Claude, Cursor, ChatGPT and anything else that speaks
MCP authorization -- there is **no URL to copy**. `/connect` says so: it shows the key you
connected and tells you to go back to your assistant. There is a link on it for the case where a
second client cannot sign in and does need a connect URL.

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

# 3. Sign in in a browser, paste a key, press Reveal and copy the connect URL.

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

## A fifth way: sign in from inside your MCP client

An MCP client only *starts* a sign-in when a request comes back `401` with a
`WWW-Authenticate: Bearer resource_metadata=…` header. Nothing else on the wire tells it one is
available. Until 2026-09-09 `/mcp` never sent that header, so a caller with no key got a 200
whose body said `needs_api_key`: correct JSON, invisible to every client's auth machinery, and
the user saw "the tool failed" with nowhere to click.

Now `/mcp` challenges, but only a caller who brought nothing, and only on a call that would spend something:

| URL | Behaviour |
|---|---|
| `https://flights.flightpowers.com/mcp` | **The one to use.** A RapidAPI key in a header, on the query string or in a Smithery config blob, an `fpk_` connect token or an `fpo_` access token: all served exactly as before. Nothing at all: `initialize`, `tools/list` and the rest of the read-only handshake are still answered, and `tools/call` gets `401` + the challenge, so your client offers a Sign in button. |
| `https://hotels.flightpowers.com/mcp` | The same, for hotels. |
| `…/mcp/oauth` | The same server with the sign-in demanded on request one. For clients whose auth mode is fixed when a server is added, and for connectors saved on that URL before the change. |

Same tools, same product-per-hostname routing, same everything else. The alias is the identical
tool registry behind a token check, not a second copy of the server.

**The property that protects paying integrations:** a request carrying a credential is never
challenged, and the credential order is unchanged (header, query, config blob, then OAuth
identity, then a connect token, then the `RAPIDAPI_KEY` env fallback). A *wrong* key is not
"nothing" either: it reaches the tools and comes back as the precise error RapidAPI gave,
because replacing that with a sign-in prompt would be a worse answer. A deployment with
`RAPIDAPI_KEY` set serves keyless callers off its own plan on purpose and is never challenged.
`MCP_REQUIRE_AUTH=off` turns the challenge off entirely, with no deploy.

**Discovery stays open, and that was learned the hard way.** For a few hours on 2026-09-09 the
challenge covered `initialize` and `tools/list` too. Glama re-checks every connector hourly by
opening an MCP connection and listing its tools, with no credentials; both paid listings were
marked unhealthy and ranked down the same evening, and Smithery's release scan, mcpservers.org
and M8ven probe the same way. So the line is drawn at spending, not at connecting: `initialize`,
`notifications/initialized`, `ping`, `tools/list`, `prompts/list` and `resources/list` are served
to anybody (`src/discovery.py`), and everything else needs a key or a sign-in. A batch with a
`tools/call` in it, an oversized body and an unparseable one are all challenged — the allowlist
fails closed. `/mcp/oauth` still challenges everything, which is the URL to give a directory
that wants a server always requiring auth, and the public, unauthenticated
`/.well-known/mcp/server-card.json` is still there for a scanner that reads a card instead.

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

### Keeping the registration table honest

`/oauth/register` is open, because the MCP spec requires it and because a `client_id` on its own
authorises nothing: every flow through one still ends at a consent page a signed-in human has to
press a button on. Open is not the same as unlimited, so three things stand behind it.

| Mechanism | Where it lives | What it stops |
|---|---|---|
| Rate limit | in memory, per instance | a burst: 10 registrations per address per 10 minutes, 60 token requests per minute, 10 `/connect/save` per hour. Over the limit is `429` with `Retry-After`. |
| Daily caps | Postgres, so every instance agrees | a slow drip: 30 registrations per address per day. The global cap is 5,000 a day -- a backstop against unbounded rows, not a defence: a global number set near real traffic is a lever an attacker pulls to refuse every new Claude or Cursor user for a day. Crossing 500 in a day logs and refuses nothing. `MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY`, `MCP_OAUTH_DCR_MAX_PER_DAY` and `MCP_OAUTH_DCR_WARN_PER_DAY` move them without a deploy. |
| Sweep | on `/oauth/register`, at most once every 15 minutes per instance | the litter: expired codes and tokens, and registrations that never became an authorization within **7 days**. A client with a live token, an outstanding code, or a consent page that has been rendered for it is never swept. |

The rate limit is per INSTANCE. On Vercel that means N warm instances allow up to N times those
numbers between them, and a cold start starts the counters at zero. That is why the durable caps
exist as well: they are counted in the database, where the number is the same everywhere.

The address every one of these is keyed on comes from `x-real-ip` first -- Vercel sets it to the
peer it accepted -- and otherwise from the LAST usable entry of `x-forwarded-for`, skipping hops
that can only be internal. Never entry 0: proxies append on the right, so the left-most entry is
whatever the caller wrote, and reading it would make every limit here one header away from being
bypassed. A request with neither header shares one bucket named `unknown`, which is rate limited
as a single caller and is not subject to the durable per-address cap (one missing header on the
edge must not lock the whole server out for a day).

A registration is stamped as in-use when its consent page is RENDERED, not only when the human
presses Approve. MCP clients commonly register when they are installed and authorize days later,
and the sweep must not delete a row while its consent page is on screen -- there is no foreign key
from codes or tokens back to the client, so the exchange that followed would fail
`invalid_client` with nothing naming the cause.

Requires one migration:

```bash
psql "$DATABASE_URL" -f migrations/003_mcp_oauth_hygiene.sql
```

### Refresh tokens rotate, and a replay revokes the family

A refresh token is single-use: exchanging it issues a new pair and retires the one presented.
The retired row is **kept and stamped**, not deleted, because a deleted row and a token that was
never issued look identical -- and telling those apart is the point. Presenting an
already-rotated refresh token means either a client that lost the response or a copy in somebody
else's hands, and OAuth 2.1 §4.14.2 says to assume the second: the answer is `invalid_grant`, and
every token descended from that authorization is deleted. The honest client signs in again; the
thief's access token stops working at the same moment.

With one deliberate exception, for the case that is almost always the innocent one: the FIRST
replay of the token we just rotated, from the same client, within 10 seconds, is answered with
the pair that rotation already issued. It is an idempotent retry -- nothing new is created -- and
it means a client whose response was lost to a dropped connection is not silently signed out. A
second replay, or one after the window, is the real thing and still kills the family. The window
is per instance and in memory, so a miss simply falls through to the conservative answer.

Revoking a refresh token through `/oauth/revoke` takes its access tokens with it, for the same
reason (RFC 7009 §2.1) -- and only if the token was issued to the client asking, which is the
other half of that section. A client presenting somebody else's token still gets `200` (§2.2) and
nothing is revoked.

### A client_id can be a URL

`client_id_metadata_document_supported: true` is advertised in
`/.well-known/oauth-authorization-server`. A client may use an **https URL** as its `client_id`;
the document at that URL lists its `redirect_uris`, and we fetch and check it per flow instead of
writing a registration row. Smithery asks for this before it will proxy a remote OAuth server.

What is checked, every time: https only, a public hostname (no IP literals, no `localhost`, no
credentials in the URL), the hostname **resolved** and every address it answers with required to
be public unicast (a name is not a control: `127.0.0.1.nip.io` has a dot in it and points at
loopback), no redirect followed, a 5-second timeout, a 64 KB cap enforced while the body is read
rather than after it is buffered, a `client_id` inside the document that matches the URL if it is
present, and -- the one that matters -- the `redirect_uri` in the request must be listed in the
document. The lookup is rate limited on its own (60 per address per 10 minutes), because it is
the only outbound fetch in this server that a caller can trigger before signing in. Dynamic registration is unchanged
and still the default: a `client_id` that is not an https URL is looked up in the table exactly as
before.

## Tools

| Tool | What it does |
|---|---|
| `search_oneway_flights` | Real-time one-way fares. Input: origin IATA, destination IATA **or a list**, and either one departure date or a date range. Returns price, airline, duration, stops, `buy_link`, and Google's historical price range so you can judge the fare. Use for any one-way question, including open-ended ones: one call with a range, never one call per date. |
| `search_roundtrip_flights` | Real-time round-trip fares priced as **paired legs**, not two one-ways. Input: origin, destination(s), a departure date or range, and either a `return_date` or a trip length in `nights` (a number or a list like `[5,6,7]`). Returns total price, per-leg airline/stops/duration, and one `buy_link` for the trip. |

The hotels deployment serves `search_hotels`, `find_hotel_by_name` and `compare_hotel_rates` instead; see [Hotels: `providers` and `compare_hotel_rates`](#hotels-providers-and-compare_hotel_rates).

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

## Hotels: a check-in range

`POST /search` prices exactly one stay, so "cheapest three nights in Rome in May" used to be
31 tool calls -- or, in practice, one call on a date the model picked and an answer presented as
the cheapest. Both hotel search tools now take the flights shape instead:

```python
search_hotels(
    destination: str,
    checkin_date: str | None = None,        # one stay: this plus checkout_date
    checkout_date: str | None = None,
    checkin_date_from: str | None = None,   # or a range: this, checkin_date_to and nights
    checkin_date_to: str | None = None,
    nights: int | list[int] | None = None,  # 3, or [2, 3, 7] to price several lengths
    max_searches: int | None = None,        # cap the billed requests this call may make
    ...
)
```

Same machinery as the flights fan-out (`src/fanout.py`): one backend call per stay, capped at
`max_searches_per_tool_call` (30, hard max 60), **sampled evenly across the range** when it does
not fit, and reported in `search_coverage`. `nights` derives each check-out date, so it replaces
`checkout_date` rather than joining it. A fixed `checkout_date` against a range of check-in dates
is allowed and means "out on the 4th, whenever I arrive"; the impossible pairs are dropped.

The response is bounded on purpose. Every property of every stay is ~25 KB per stay (measured:
25,892 bytes for 25 properties, 18,989 of them URLs), so each stay reports its cheapest property,
its per-night rate and its median, and the **full property list comes back for the cheapest stay
only**:

```jsonc
{
  "results": [ /* every property of the CHEAPEST stay, upstream rows untouched */ ],
  "result_count": 18,
  "results_for_stay": {"checkin_date": "2026-05-12", "checkout_date": "2026-05-15", "nights": 3},
  "stays": [
    {
      "checkin_date": "2026-05-01", "checkout_date": "2026-05-04", "nights": 3,
      "search_status": "ok", "reason": "ok",
      "property_count": 22, "priced_count": 19,
      "cheapest_total": 411.0, "price_per_night": 137.0, "median_total": 690.0,
      "currency": "USD",
      "cheapest": { /* the row, minus its image URL */ }
    },
    {"checkin_date": "2026-05-02", "search_status": "degraded", "reason": "search_failed",
     "property_count": null, "priced_count": null, "cheapest": null},
    {"checkin_date": "2026-05-03", "search_status": "not_searched", "reason": "not_searched",
     "property_count": null, "cheapest": null}
  ],
  "cheapest_overall": {"checkin_date": "2026-05-12", "total": 305.0, "price_per_night": 101.67,
                       "currency": "USD", "property": { /* ... */ }},
  "search_status": "partial",
  "search_coverage": {
    "requested_combinations": 31,
    "searched_combinations": 15,
    "truncated": true,
    "max_searches_per_request": 30,
    "stays_searched": [{"checkin_date": "2026-05-01", "checkout_date": "2026-05-04"}, "..."],
    "checkin_dates_searched": ["2026-05-01", "..."],
    "note": "This request expanded to 31 stays, above the ..."
  },
  "api_usage": {"requests_used_by_this_call": 15, "note": "... Each stay -- one check-in date paired with one length -- is one billed request."}
}
```

`reason` on a stay is a fact about our pipeline, never a guess about the hotel:

| `reason` | `search_status` | Means |
|---|---|---|
| `ok` | `ok` | priced |
| `no_availability` | `empty` | searched, answered, nothing came back |
| `no_price` | `empty` | properties came back, none carried a price (`available: false` lands here) |
| `search_failed` | `degraded` | the search errored, so **nothing is known** -- not "no rooms" |
| `not_searched` | `not_searched` | the cap sampled it away |

Counts are `null` rather than `0` on the last two: zero reads as "nothing there", and neither case
knows that. Top-level `search_status` is `ok` / `partial` / `empty` / `degraded` over the stays;
every stay failing raises instead of answering with an empty list.

Two deliberate refusals: a check-in range with `providers` naming more than one source (a fan-out
times a per-source fan-out, billed to two subscriptions, that neither `search_coverage` nor
`api_usage` can describe honestly today), and `max_searches` on a single stay, which would silently
do nothing.

**A single stay is byte-identical to what it was before this existed** -- same request body, same
response keys, no `stays`, no `search_coverage`, no `search_status`. Asserted in
`tests/test_hotel_date_range.py::TestTheOldShapeIsUntouched` against a frozen expectation captured
from the previous code.

## Hotels: `providers` and `compare_hotel_rates`

The hotels deployment (`hotels.flightpowers.com`, the same code selected by the `Host` header)
serves three tools. `search_hotels` and `find_hotel_by_name` also take a check-in range (above);
`search_hotels` gained one optional argument for sources and there is one new tool.

| Tool | What it does |
|---|---|
| `search_hotels` | Live rates for a destination and dates, or for every stay a check-in range expands to. `providers` names the sources to price on: `["booking"]` (the default), `["airbnb"]`, or both. |
| `find_hotel_by_name` | One named property, Booking.com only. Airbnb's room page carries no price, so a name lookup there would resolve to something that cannot be priced. |
| `compare_hotel_rates` | The same stay priced on every source you have a key for, one row per source: cheapest total, median total, how many places were priced, currency, and when the rows were read. |

### The default did not move

`search_hotels` with no `providers` argument sends the same upstream request it always sent, to the
same host, and answers with the same keys. So does `providers: ["booking"]`. Both are asserted in
`tests/test_providers.py::TestTheDefaultDidNotMove`, upstream request body included -- a default is
only a default if keeping it costs nothing.

Naming a second source changes the response shape, and only then:

```jsonc
{
  "results": [ /* every source's rows, each carrying "provider" and "rating_scale" */ ],
  "result_count": 3,
  "providers":        [ /* one row per source that was CALLED */ ],
  "providers_skipped":[ /* one row per source that was NOT, with a subscribe_url */ ],
  "caveats":          [ /* what to read before calling one source cheaper */ ],
  "api_usage":        { "requests_used_by_this_call": 2 }
}
```

### Where each source is called

* **`booking`** goes straight to `booking-live-api.p.rapidapi.com` on the caller's key, billed by
  RapidAPI to their own subscription. Unchanged.
* **`airbnb`** goes through our own front door, `POST https://api.flightpowers.com/v1/hotels/search`
  with `provider: "airbnb"` in the body (`flight_rabbi` #472), because the Airbnb backend is not on
  the RapidAPI edge. The call carries the caller's key as `x-rapidapi-key` and identifies itself
  with `X-FP-Client: mcp-hotels/<version>` -- the front overwrites body attribution with its own
  conclusion, so the header is the only thing that says who called (rule 11). Override the origin
  with `API_FRONT_BASE_URL` for a preview front; it holds no credential.

**The Airbnb listing does not exist on RapidAPI yet, and the front ships with the provider off**, so
today a real `providers: ["airbnb"]` call comes back as a `degraded` row saying so. That is the
designed answer, not a bug: an unmetered source reachable by anyone with any valid key is a gateway
we pay for.

### Four rules the implementation holds

1. **A source you have no key for is never called on ours.** It is named in `providers_skipped` with
   `reason` (`no_key`, `not_subscribed`, `key_rejected`) and the URL where you subscribe. Each
   listing is a separate subscription, so a `403` from the Hub means "not subscribed to *that*
   listing", not "bad key".
2. **A degraded source is a named row, not a hole.** `search_status: "degraded"`, `count: null`, no
   prices -- and the other source's rows still come back. "Booking did not answer" and "Booking had
   nothing" are opposite answers and must be sayable as different sentences.
3. **`cheapest_total` and `median_total` cover only the rows that carry a price**, and `count` is
   that number. A `price_string` is never parsed into a number.
4. **No cross-currency arithmetic.** Every source is asked for the same currency; if two answer in
   different ones, each row keeps its own and `caveats` says the totals are not comparable. Nothing
   is converted.

### Keys, per source

Almost everybody has one RapidAPI key subscribed to several listings, and that key is used for every
source with no extra configuration. A caller who genuinely holds two can name one per source, and the
source-scoped name wins:

```
x-rapidapi-key-airbnb: <key>        # header
?rapidapi_key_airbnb=<key>          # query
?config=<base64 {"rapidApiKeyAirbnb": "<key>"}>   # Smithery blob
```

The order is the one `src/credentials.py` already documents -- source-scoped names, then the
unscoped specific names, then the config blob, then generic names last and skipped entirely when a
`config` parameter is present, because a gateway's own key under a generic name is not ours.

### `filters` and `price_as_seen_from` are Booking-only

On a mixed search they are sent to Booking and not to the front. On an Airbnb-only search they are
**refused**, not dropped: a silently discarded filter returns more properties than you asked for and
nothing says so.

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
`GET /metrics`, `GET /metrics/calls?hours=24`,
`GET /.well-known/mcp/server-card.json` (see below).

### The static server card

`GET /.well-known/mcp/server-card.json` returns this deployment's metadata as a standalone
JSON document: `serverInfo`, `description`, `transport`, `capabilities`, `authentication`,
`instructions`, and the full `tools` and `prompts` lists exactly as `tools/list` serialises
them. Public, unauthenticated, `Cache-Control: public, max-age=3600`, open CORS.

It exists because the URL we publish in directories is `/mcp/oauth`, which always answers
401, so an automated scanner pointed at THAT url cannot read the tool list off the wire.
(Since 2026-09-09 a scanner pointed at `/mcp` can: read-only discovery is served without a
credential — see `src/discovery.py`.) Smithery's publish
page names this document as the way out: "If automatic scanning can't complete (auth wall,
required configuration, or other issues), you can provide server metadata manually via a
static server card at /.well-known/mcp/server-card.json". The field list follows
[SEP-1649](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1649).

Two things worth knowing:

* The tool list is read from the LIVE registry at first request, never written out by hand,
  so it cannot drift from what `tools/list` returns. `tests/test_server_card.py` compares
  the two on both products.
* It is per hostname, like everything else here: `hotels.flightpowers.com` returns the hotel
  card, both flights hostnames return the flights card, and every URL inside is on that
  product's own origin. `transport.endpoint` is `/mcp/oauth` where OAuth is configured, with
  the keyed `/mcp` endpoint listed under `_meta` as an alternative.

```bash
curl -s https://flights.flightpowers.com/.well-known/mcp/server-card.json | python3 -m json.tool
curl -s https://hotels.flightpowers.com/.well-known/mcp/server-card.json  | python3 -m json.tool
```

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
