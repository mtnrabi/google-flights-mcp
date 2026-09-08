# Privacy Policy: {{PRODUCT}} (paid, ad-free)

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

A hosted Model Context Protocol (MCP) server exposing these tools:

{{TOOL_BULLETS}}

{{TOOL_BEHAVIOUR}} Search results are
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
  parameter or base64 `config=` blob, or, last, and
  normally unset, a `RAPIDAPI_KEY` value in the server's own environment.
- **What is done with it.** It is held in memory for the duration of that one
  request and sent to RapidAPI as the `x-rapidapi-key` header on each upstream
  search request that the call makes.
- **What is not done with it.** It is never written to any log, never included
  in an error message, never returned in a tool response, and never cached or
  reused for a later request. A key supplied on a request is held in memory for
  that request and then discarded; it is **not stored**.
- **The one exception, and it is yours to choose.** If, and only if, you use
  the optional `/connect` page (section 2a), the key you paste there is stored,
  encrypted, so that later requests can use it without you pasting it again. A
  server that never sees you use `/connect` stores nothing about you.
- **What is recorded about it.** Only *which mechanism supplied it*: the
  literal values recorded are strings such as `header:x-rapidapi-key`,
  `query:rapidapi_key`, `env:RAPIDAPI_KEY`, or `none`. This field exists so
  that "users cannot work out how to pass a key" is distinguishable from "users
  are not trying". It contains no part of the key value.

If a key is absent, the server returns instructions instead of running a search,
and spends nothing.

---

## 2a. The optional `/connect` page

Some deployments of this server offer a page at `/connect`. It is optional in
two senses: not every deployment has it (`/health` reports `connect_enabled`,
and where it is off the page does not exist), and where it does exist, using it
is your choice, every other way of passing a key keeps working untouched.

If you use it:

- **You sign in with Google.** The server asks Google for two things and
  nothing else: `openid` and `email`. It does not ask for, and cannot see,
  your Google Drive, Calendar, contacts, documents, profile picture or
  anything else in your account.
- **What is kept from that sign-in.** Your Google account identifier (`sub`)
  and your email address. Nothing else. The email is kept so you can see which
  account a key is attached to; it is not added to any mailing list by this
  server.
- **Your RapidAPI key is encrypted before it is written down.** AES-256-GCM,
  under a key held in the server's environment and never in the database. The
  stored record holds the ciphertext, the last four characters of the key (the
  only readable fragment, so the page can show you which key is connected),
  your account identifier and your email address. Anyone holding a copy of the
  database and not the encryption key holds nothing usable.
- **What you get back is a token, not your key.** The connect URL the page
  gives you carries a revocable `fpk_…` token. It cannot be turned back into
  your RapidAPI key, and on its own it authorises nothing: it resolves to the
  stored record, and if that record is gone it resolves to nothing.
- **Two cookies, both first-party and functional.** One short-lived cookie
  carries the sign-in round trip to Google; one carries your signed-in session
  for up to an hour. Both are `HttpOnly` and scoped to `/connect`, neither is
  sent with an MCP request, and neither is used for analytics, advertising or
  tracking of any kind. There are no third-party cookies anywhere on this
  service.
- **Deleting it is one button.** "Disconnect" on `/connect` **deletes** the
  stored record, the ciphertext included, not a flag set on a row that keeps
  it, and every connect token for that account stops resolving immediately.
  Your RapidAPI account and subscription are untouched; only our copy of the
  key is removed.

## 2b. Signing in from inside your MCP client (`/mcp/oauth`)

Where `/connect` is available, so is a second MCP endpoint at `/mcp/oauth`
(`/health` reports `oauth_enabled`). Instead of pasting a URL with a token in
it, your MCP client signs you in itself: it registers with this server, opens
a browser, and you approve that client by name.

It uses exactly the same Google sign-in and the same encrypted key record that
section 2a describes, there is no second identity and no second copy of your
key. What is additionally written down is only what makes the sign-in work:

- **The client's registration.** The name it gave, the URL it asked to be sent
  back to, and an identifier we generated for it. No personal data.
- **Short-lived grants tied to your account.** An authorization code (valid for
  ten minutes, usable once), an access token (one hour) and a refresh token
  (thirty days), each stored as a one-way hash, the database never holds a
  value that could be replayed, alongside your Google account identifier and
  the client that was approved.
- **What the client can do.** Run searches billed to your own RapidAPI plan.
  It never receives your RapidAPI key, and there is nothing else these tokens
  authorise.
- **Deleting it is the same button.** "Disconnect" on `/connect` deletes your
  stored key *and* drops every access and refresh token issued for your
  account, in the same action. A client can also revoke its own token at
  `/oauth/revoke`. Expired codes and tokens are deleted; nothing about a
  finished sign-in is kept for analytics.

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
| `error` | An error label such as `no_api_key`, `auth` or `quota`; a validation message when the request was rejected before any search ran; otherwise null. A validation message quotes the value that failed validation, an unparseable date or trip length, for example |

If the deployment is configured with a writable log path, the same line is also
appended to a local file. On the current hosting platform that path is not
writable, so standard output is the record.

---

## 4. What is counted

Aggregate integer counters are incremented per call:
`tool_calls`, `upstream_calls`, `upstream_failures`, `results_returned`,
`truncated_calls`, `errored_calls`, `unauthenticated_calls`, plus per-UTC-hour
totals of `tool_calls` and `upstream_calls`.

These are plain numbers with no per-caller dimension: there is no key, no
identifier, and no way to attribute a counter to anyone. By default they live in
process memory and reset when the process recycles. If an Upstash Redis store is
configured, they are stored there as integers under the key prefix `gfpaid`;
hourly buckets expire automatically after 35 days, and lifetime totals are
running integers.

---

## 5. What is **not** collected

The server does not collect, log, store, or transmit any of the following:

- **Your API key value**, see section 2.
- **IP addresses.** This server logs, stores and forwards none. No IP address
  appears in any record it writes, and none is sent upstream. (The hosting
  platform terminates the network connection and keeps its own request logs,
  see section 6.)
- **Any user identity**, unless you signed in on `/connect` or through
  `/mcp/oauth`: the tool path itself has no account, no user ID, no session
  ID, no name, no email address and no device or client identifier. What
  sections 2a and 2b describe is the only identity this service ever holds,
  it exists only because you chose to create it, and Disconnect deletes it.
- **Your search parameters.** {{SEARCH_PARAM_NOUNS}} are used to perform the
  search and are then discarded. Only the *count* of requested combinations is
  recorded, not the values, with one exception: when a request is rejected as
  invalid before any search runs, the validation message goes into the `error`
  field described in section 3, and that message quotes the single value that
  failed (a malformed date or trip length, for example).
- **{{RESULT_NOUN}}.** {{RESULT_SENTENCE}}
- **Tracking cookies, trackers, analytics SDKs, fingerprinting, advertising.**
  This server serves no ads and embeds no third-party tracking of any kind. The
  MCP endpoint sets no cookies at all. The only cookies that exist anywhere on
  this service are the two functional, first-party, `HttpOnly` ones described
  in section 2a, and they only exist if you use `/connect`.

There is no profiling, no automated decision-making, and no sale or sharing of
data. There is no personal data to sell.

---

## 6. Third parties

- **RapidAPI / {{UPSTREAM_API}}.** Each search is forwarded to RapidAPI
  with your key so that it is billed to your own subscription. RapidAPI and the
  API provider handle that request under their own terms and privacy policies.
- **Google**, only if you use `/connect` (section 2a). Signing in sends you to
  Google's own consent screen; Google handles that under its own privacy
  policy. The service asks for `openid` and `email` and receives nothing else.
  If you never use `/connect`, this service never contacts Google about you.
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

- `GET /health`: public and unauthenticated. Returns service status, the
  public MCP URL, the signup URL, whether ads are served (always `false`), and
  whether a server-side fallback key is configured. It contains no caller data.
- `GET /metrics` and `GET /metrics/calls`: the aggregate counters from
  section 4, protected by a `x-metrics-token` header when a token is
  configured. They contain no caller data.
- `GET /.well-known/openai-apps-challenge`: a domain-verification token, or
  404 when unconfigured.

---

## 8. Retention and deletion

- Search parameters and results: not retained.
- API keys passed on a request: not retained.
- A key connected through `/connect`: retained, encrypted, until you press
  Disconnect. There is no expiry sweep: the record lasts as long as you want
  it to and no longer.
- The Google account identifier and email address behind a connected key: the
  same lifetime as the key, and deleted with it.
- `MCP_CALL` log lines: retained by the hosting platform under its runtime log
  retention. They contain no personal data, so there is nothing in them to
  attribute to, or delete for, an individual.
- Counters: hourly buckets expire after 35 days; lifetime totals are aggregate
  integers with no personal dimension.

If you have not used `/connect`, no personal data is collected, so there is no
account to close and no per-user deletion or access request that can be
meaningfully fulfilled: there is no record keyed to you to find. If you have,
"Disconnect" on that page is the deletion, it is immediate, and it needs no
request to anybody. Either way, if you believe a record about you exists that
this policy does not describe, write to the contact address below and we will
investigate.

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
