# Terms of Service — {{PRODUCT}} (paid, ad-free)

**Service:** `https://{{HOST}}/mcp`
**Registry name:** `{{REGISTRY_NAME}}`
**Effective date:** 2026-08-17
**Contact:** mtnrabi@gmail.com

By connecting an MCP client to this server or calling its tools, you agree to
these terms. If you do not agree, do not connect to it.

"Paid" in the title distinguishes this deployment from the free, ad-supported
one; it means bring-your-own-key. This server charges you nothing itself —
every search is billed to your own RapidAPI subscription. See section 2.

---

## 1. What you get

A hosted Model Context Protocol server exposing two tools:

{{TOOL_BULLETS}}

Both take a date range and a list of destinations and expand them internally
into individual searches. One user intent is one tool call. The fan-out cap is
30 searches per call. A per-call `max_searches` argument can lower that cap for
a single call but cannot raise it; 60 is the hard ceiling that the
deployment-wide setting itself cannot exceed.

The server serves no advertising.

**Non-affiliation.** This is an independent service that returns publicly
available {{DATA_NOUN}} pricing. It is not affiliated with, endorsed by, or sponsored by {{NOT_AFFILIATED}}. Airline, airport and travel-brand names appear only as factual
descriptions of search results.

---

## 2. You bring your own key, and you pay for your own searches

This server does not resell {{DATA_NOUN}} data and does not pay for your searches.
Every search is billed to **your own RapidAPI subscription** for the {{UPSTREAM_API}}.

- Supply your key via the `x-rapidapi-key` header (preferred), a
  `?rapidapi_key=` query parameter on the server URL, or your client's API-key
  field if it offers one. Prefer the header: a key placed in a URL ends up in
  server and proxy logs along the way.
- Without a key, the server returns instructions and runs no search. It does
  not spend anything on your behalf.
- Every successful response carries an `api_usage` block. It always reports
  `requests_used_by_this_call`, and adds `plan_requests_remaining` and
  `plan_requests_limit` whenever RapidAPI returns its rate-limit headers for
  your plan — so the cost of a call is visible in the call itself.
- You are responsible for your RapidAPI plan, its quota, and its charges. Cost
  of a fan-out is roughly one billed upstream request per date/destination
  combination searched; check `api_usage` for the actual figure.
- Your relationship with RapidAPI and with the underlying API provider is
  governed by their terms, not these.

Keep your key to yourself. Do not embed a key you do not own, and do not share a
connector URL that carries your key in the query string.

---

## 3. Acceptable use

You may not:

- attempt to defeat the fan-out caps or per-call limits, or issue automated
  traffic designed to degrade the service for others;
- use another party's RapidAPI key without authorisation;
- attempt to gain access to the server's infrastructure, environment variables,
  operational endpoints, or other users' traffic;
- represent the service, or data obtained through it, as an official {{NOT_AFFILIATED}}
  product, an offer of sale, or a booking confirmation;
- resell or redistribute the service in a way that circumvents the requirement
  that each caller supply their own key;
- use the service for anything unlawful, or in breach of the underlying API
  provider's or RapidAPI's terms.

We may throttle, suspend, or block access that breaches this section or that
threatens the stability of the service.

---

## 4. About the data returned

- **Fares change constantly and go stale within minutes.** Every result is a
  snapshot of the moment it was fetched. Do not cache fares, do not reuse an
  earlier result, and do not present a previously fetched fare as current. If
  you display a fare, display when it was fetched.
- **Results are informational, not an offer.** This service does not sell
  tickets, hold inventory, take payment, or make bookings. Availability and
  price are confirmed only at the airline or booking site, via the `buy_link`
  in each result.
- **An empty result set is a valid answer**, meaning no flights were found for
  that route and those dates. It is not an error.
- **Price insight fields** (`price_range_in_relation_to_other_periods`,
  `price_insights_low`, `price_insights_high`) are the upstream provider's
  historical characterisation of a route and period. They are context, not a
  prediction or financial advice.
- Data is passed through from the upstream API. Its accuracy, completeness and
  availability are not warranted by this service.

---

## 5. Availability and changes

The service is provided on a best-effort basis. No uptime, latency, or
throughput is promised, and none is stated anywhere in these terms. Tools,
parameters, caps and response shapes may change; breaking changes will be
reflected in the published documentation. The service may be suspended or
discontinued at any time.

---

## 6. Privacy

See the Privacy Policy published alongside these terms. In short: your API key
is forwarded to RapidAPI and never stored; the server logs a call count, the
tool name, a duration, a result count, and which mechanism supplied the key; it
does not log the key, your IP address, your search parameters, or any user
identity.

---

## 7. Warranty disclaimer

THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING WITHOUT LIMITATION ANY WARRANTY OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, ACCURACY, OR
NON-INFRINGEMENT.

---

## 8. Limitation of liability

To the maximum extent permitted by law, the operator is not liable for any
indirect, incidental, special, consequential, or exemplary damages, nor for lost
profits, lost bookings, missed fares, travel disruption, or RapidAPI charges
incurred through your use of the service, whether or not foreseeable. To the
extent liability cannot be excluded, it is limited to the amount you have paid
the operator for the service, which for this server is zero — the service itself
charges you nothing and your searches are billed by RapidAPI, not by us.

---

## 9. Indemnity

You agree to indemnify the operator against claims arising from your use of the
service in breach of these terms or of applicable law.

---

## 10. Termination

You may stop using the service at any time by removing the connector. We may
terminate or suspend access for breach of section 3, or where required by law or
by an upstream provider.

---

## 11. Changes to these terms

Updated terms will be published at this URL with a new effective date.
Continued use after publication constitutes acceptance.

---

## 12. Contact

mtnrabi@gmail.com
