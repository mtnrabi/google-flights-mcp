# Privacy Policy — {{PRODUCT}} (paid, ad-free)

**Service:** `https://{{HOST}}/mcp`
**Registry name:** `{{REGISTRY_NAME}}`
**Effective date:** 2026-08-17
**Contact:** mtnrabi@gmail.com

This policy describes exactly what the hosted MCP server at
`{{HOST}}` does with data. It is written from the
server's source, not from a template. Where the server collects nothing, this
policy says nothing is collected rather than reserving a right we do not use.

---

## 1. What the service is

A hosted Model Context Protocol (MCP) server exposing two tools:

{{TOOL_BULLETS}}

Both accept a date range and a list of destinations, expand them internally
into individual searches (30 per call by default; a per-call `max_searches`
argument can lower that figure for a single call but cannot raise it, and 60 is
the hard ceiling the deployment-wide setting itself cannot exceed), and forward
each search to the {{UPSTREAM_API}} on RapidAPI. Search results are
returned to the caller in the tool response and are not retained.

**Non-affiliation.** This is an independent service that returns publicly
available {{DATA_NOUN}} pricing. It is not affiliated with, endorsed by, or sponsored by {{NOT_AFFILIATED}}.

---

## 2. Your RapidAPI key

Every search on this server is billed to **the caller's own RapidAPI
subscription**. The key is supplied per request, and this is what happens to
it:

- **Where it is accepted from.** The `x-rapidapi-key` request header
  (preferred), an `authorization: Bearer` or `x-api-key` header, a query
  parameter on the connector URL (`rapidapi_key`, `rapidapi-key`,
  `rapidapikey`, `api_key`, `apikey`, or `key`), a Smithery-style `config.`
  parameter or base64 `config=` blob, or — last, and
  normally unset — a `RAPIDAPI_KEY` value in the server's own environment.
- **What is done with it.** It is held in memory for the duration of that one
  request and sent to RapidAPI as the `x-rapidapi-key` header on each upstream
  search request that the call makes.
- **What is not done with it.** It is **never stored**, never written to any
  log, never included in an error message, never returned in a tool response,
  and never cached or reused for a later request. There is no database of keys
  because there is no database of anything per-caller.
- **What is recorded about it.** Only *which mechanism supplied it* — the
  literal values recorded are strings such as `header:x-rapidapi-key`,
  `query:rapidapi_key`, `env:RAPIDAPI_KEY`, or `none`. This field exists so
  that "users cannot work out how to pass a key" is distinguishable from "users
  are not trying". It contains no part of the key value.

If a key is absent, the server returns instructions instead of running a search,
and spends nothing.

---

## 3. What is logged

One JSON line per **tool call** (never one per upstream request) is written to
the server's standard output, prefixed `MCP_CALL `. It contains exactly these
fields and nothing else:

| Field | Meaning |
|---|---|
| `ts`, `iso` | Time of the call (epoch seconds and UTC timestamp) |
| `tool` | one of {{TOOL_NAMES}} |
| `requested_combinations` | How many date/destination combinations the call asked for (a number) |
| `upstream_calls` | How many RapidAPI requests the call actually made |
| `upstream_failures` | How many of those failed |
| `results_returned` | How many results were returned (a number) |
| `duration_ms` | How long the call took |
| `truncated` | Whether the fan-out cap trimmed the search |
| `credential_source` | Which mechanism supplied the key (see section 2) |
| `error` | An error label such as `no_api_key`, `auth` or `quota`; a validation message when the request was rejected before any search ran; otherwise null. A validation message quotes the value that failed validation — an unparseable date or trip length, for example |

If the deployment is configured with a writable log path, the same line is also
appended to a local file. On the current hosting platform that path is not
writable, so standard output is the record.

---

## 4. What is counted

Aggregate integer counters are incremented per call:
`tool_calls`, `upstream_calls`, `upstream_failures`, `results_returned`,
`truncated_calls`, `errored_calls`, `unauthenticated_calls`, plus per-UTC-hour
totals of `tool_calls` and `upstream_calls`.

These are plain numbers with no per-caller dimension — there is no key, no
identifier, and no way to attribute a counter to anyone. By default they live in
process memory and reset when the process recycles. If an Upstash Redis store is
configured, they are stored there as integers under the key prefix `gfpaid`;
hourly buckets expire automatically after 35 days, and lifetime totals are
running integers.

---

## 5. What is **not** collected

The server does not collect, log, store, or transmit any of the following:

- **Your API key value** — see section 2.
- **IP addresses.** This server logs, stores and forwards none. No IP address
  appears in any record it writes, and none is sent upstream. (The hosting
  platform terminates the network connection and keeps its own request logs —
  see section 6.)
- **Any user identity** — no account, no user ID, no session ID, no name, no
  email address, no device or client identifier.
- **Your search parameters.** Origins, destinations, dates, passenger counts,
  cabin class, price limits and airline filters are used to perform the search
  and are then discarded. Only the *count* of requested combinations is
  recorded, not the values — with one exception: when a request is rejected as
  invalid before any search runs, the validation message goes into the `error`
  field described in section 3, and that message quotes the single value that
  failed (a malformed date or trip length, for example).
- **Flight results.** Fares are returned to you and not retained. Fares go stale
  within minutes, so nothing is cached or reused.
- **Cookies, trackers, analytics SDKs, fingerprinting, advertising.** This
  server serves no ads and embeds no third-party tracking of any kind.

There is no profiling, no automated decision-making, and no sale or sharing of
data. There is no personal data to sell.

---

## 6. Third parties

- **RapidAPI / {{UPSTREAM_API}}.** Each search is forwarded to RapidAPI
  with your key so that it is billed to your own subscription. RapidAPI and the
  API provider handle that request under their own terms and privacy policies.
- **Upstash Redis (optional).** If configured, it holds only the aggregate
  integer counters described in section 4.
- **Hosting platform (Vercel).** The server runs on Vercel, which captures the
  standard-output lines described in section 3 as runtime logs and retains them
  under its own log retention. Like any internet host, the platform also
  terminates the network connection and may keep its own request-level logs
  under its own policy; that layer is the platform's, not this server's, and
  this server neither reads nor uses it.

No other processor receives anything.

---

## 7. Operational endpoints

- `GET /health` — public and unauthenticated. Returns service status, the
  public MCP URL, the signup URL, whether ads are served (always `false`), and
  whether a server-side fallback key is configured. It contains no caller data.
- `GET /metrics` and `GET /metrics/calls` — the aggregate counters from
  section 4, protected by a `x-metrics-token` header when a token is
  configured. They contain no caller data.
- `GET /.well-known/openai-apps-challenge` — a domain-verification token, or
  404 when unconfigured.

---

## 8. Retention and deletion

- Search parameters and results: not retained.
- API keys: not retained.
- `MCP_CALL` log lines: retained by the hosting platform under its runtime log
  retention. They contain no personal data, so there is nothing in them to
  attribute to, or delete for, an individual.
- Counters: hourly buckets expire after 35 days; lifetime totals are aggregate
  integers with no personal dimension.

Because no personal data is collected, there is no account to close and no
per-user deletion or access request that can be meaningfully fulfilled — there
is no record keyed to you to find. If you believe otherwise, write to the
contact address above and we will investigate.

---

## 9. Children

The service is a developer API tool and is not directed to children.

---

## 10. Changes

Material changes to this policy will be published at this URL with an updated
effective date. The version in force is the one published here.

---

## 11. Contact

mtnrabi@gmail.com
