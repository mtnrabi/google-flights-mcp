# Google Flights MCP (hosted, ad-free)

Real-time flight fares with Google's own low/typical/high price verdict on every result. Hosted MCP server, nothing to clone or build.

## Remote URL

```
https://flights.flightpowers.com/mcp
```

## Sign in, or bring a key

One URL, two ways in. A client that supports MCP authorization gets a `401` with the sign-in
details and shows a **Sign in** button: the user signs in with Google and pastes a RapidAPI key
once on the `/connect` page, and nothing goes in the client config. A script or a client without
a sign-in button sends the key on the same URL instead.

## Header

```
x-rapidapi-key: YOUR_RAPIDAPI_KEY
```

## Quick install

```bash
claude mcp add --transport http google-flights https://flights.flightpowers.com/mcp --header "x-rapidapi-key: YOUR_RAPIDAPI_KEY"
```

## Get your RapidAPI key

Subscribe to the Google Flights Live API on RapidAPI (free tier available):  
**https://rapidapi.com/mtnrabi/api/google-flights-live-api**

Copy your `x-rapidapi-key` and use it in the header above.

## More info

- **Site:** https://flightpowers.com
- **Repo:** https://github.com/mtnrabi/google-flights-mcp
- **Health check:** https://flights.flightpowers.com/health

## What your agent gets

- **search_oneway_flights** (real-time one-way fares across multiple dates and destinations in one call)
- **search_roundtrip_flights** (real-time round-trip fares with Google's price insights, low/typical/high)

Every result includes buy links, price range context, and API usage reporting so you know what you spent.
