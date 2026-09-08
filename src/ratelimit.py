"""
Per-instance rate limiting for the three OAuth endpoints a stranger can reach.

What this is, and what it honestly is not
-----------------------------------------
An in-memory sliding window, one dictionary per process. On Vercel that means
**per instance**, not per deployment: N warm instances allow up to N times
these numbers between them, and a cold start starts every counter at zero.
That is stated here rather than hidden, because a limiter whose real ceiling
is unknown is worse than one whose ceiling is known and loose.

It is still worth having. The thing being stopped is one caller hammering
`/oauth/register` or `/oauth/token` from a script, and a script that finds
itself throttled on every instance it lands on has already lost the cheap
version of that attack. The durable half of the defence -- a cap on how many
client registrations can exist per day, counted in Postgres where every
instance sees the same number -- lives in `oauth.py` and `oauthstore.py`.
This file is the cheap first line, not the guarantee.

Fail closed, in two senses
--------------------------
1. Over the limit means **429 with `Retry-After`**, never "let it through and
   log it". A limiter that degrades to permissive under pressure is a limiter
   that does nothing at exactly the moment it is needed.
2. A caller whose IP cannot be determined shares ONE bucket named `unknown`.
   Bucketing unknowns separately per request would be the same as not
   limiting them, since the identity is the thing being spoofed.

The table is capped. Past `MAX_KEYS` live buckets the limiter prunes expired
ones and, if that is not enough, refuses new keys until the window rolls --
memory is a shared resource on a serverless instance, and an unbounded
defensive dictionary is its own denial of service.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The bucket every caller whose address we cannot establish shares. A name
#: rather than an empty string so that the value is legible in a log line and
#: in `registered_ip`, and so that "we did not know" is never mistaken for an
#: address somebody actually sent.
UNKNOWN_IP = "unknown"

#: How many distinct buckets one process will track before it starts refusing
#: new ones. Each entry is a short list of floats; 20k of them is a few MB.
MAX_KEYS = 20_000


@dataclass(frozen=True)
class Limit:
    """`count` requests per `window` seconds, for one named endpoint."""

    name: str
    count: int
    window: float

    def retry_after(self) -> int:
        # Whole seconds, at least one: `Retry-After: 0` reads as "retry now",
        # which is the opposite of what a 429 means.
        return max(1, int(self.window))


#: A client registers once per install. Ten in ten minutes from one address is
#: already a client in a retry loop; a hundred is a script.
REGISTER = Limit("register", 10, 600)
#: Refreshes are hourly per client, and an interactive exchange happens once.
#: Sixty a minute leaves room for a burst of parallel clients behind one NAT.
TOKEN = Limit("token", 60, 60)
#: Every save runs one real request against the user's own RapidAPI plan, so
#: this one protects the USER's money as much as our database.
CONNECT_SAVE = Limit("connect_save", 10, 3600)
#: The consent page resolves a CIMD client_id over the NETWORK, before the
#: caller has signed in and before anything else costs us a query. That makes
#: it the one unauthenticated outbound fetch in this server, so it gets a
#: ceiling of its own. Sixty per ten minutes is far above a human signing
#: into an MCP client (one) and far below a script using us as a fetcher.
CIMD_FETCH = Limit("cimd_fetch", 60, 600)


class RateLimiter:
    """One process's view of who has been calling what.

    Not thread-safe by lock, and it does not need to be: the ASGI server runs
    one event loop, and every mutation below happens between awaits.
    """

    def __init__(self, max_keys: int = MAX_KEYS) -> None:
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._max_keys = max_keys

    def allow(self, limit: Limit, key: str, now: float | None = None) -> bool:
        """True if this request is within the limit, and counts it if so."""
        now = now if now is not None else time.time()
        bucket = (limit.name, key or "unknown")
        hits = [t for t in self._hits.get(bucket, ()) if t > now - limit.window]
        if len(hits) >= limit.count:
            # Keep the pruned list so the window still rolls forward, but do
            # NOT record this attempt: a caller who keeps hammering would
            # otherwise extend their own lockout indefinitely, which turns a
            # rate limit into a ban nobody decided to hand out.
            self._hits[bucket] = hits
            return False
        if bucket not in self._hits and len(self._hits) >= self._max_keys:
            self._prune(now)
            if len(self._hits) >= self._max_keys:
                logger.warning(
                    "rate-limit table is full (%d keys); refusing new callers",
                    len(self._hits),
                )
                return False
        hits.append(now)
        self._hits[bucket] = hits
        return True

    def _prune(self, now: float) -> None:
        """Drop buckets whose newest hit is older than any window we use."""
        widest = max(
            REGISTER.window, TOKEN.window, CONNECT_SAVE.window, CIMD_FETCH.window
        )
        cutoff = now - widest
        for bucket in [b for b, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[bucket]

    def reset(self) -> None:
        self._hits.clear()


#: One limiter per process, shared by every product app in it. Shared on
#: purpose: `flights.` and `hotels.` are the same instance and the same
#: `/oauth/token`, so counting them separately would double every limit.
LIMITER = RateLimiter()


#: Address ranges that can only be a hop inside somebody's network, never the
#: caller. Spelled out rather than asking `ipaddress` for `is_private`,
#: because that predicate also covers the documentation ranges (192.0.2.0/24,
#: 198.51.100.0/24, 203.0.113.0/24) and those are ordinary values here.
_INTERNAL = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def usable_address(value: str) -> str:
    """`value` normalised, or "" when it cannot be a caller's own address.

    Anything that is not an IP literal is refused outright: a hostname, a
    token, `unknown` or a truncated header entry is not an address, and
    letting one through would hand the caller a bucket key of their choosing.
    """
    text = (value or "").strip().strip("[]")
    if not text:
        return ""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return ""
    # `::ffff:127.0.0.1` is loopback wearing an IPv6 hat.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if any(addr in net for net in _INTERNAL):
        return ""
    return str(addr)


def client_ip(headers: dict[str, str]) -> str:
    """The caller's address as far as this deployment can tell.

    Order, and why it is this order:

    1. **`x-real-ip`.** Vercel sets it to the peer it accepted the connection
       from, and overwrites whatever the caller sent. It is the one value
       here the platform vouches for.
    2. **The LAST usable entry of `x-forwarded-for`.** Standard XFF semantics
       are that each proxy APPENDS, so the left-most entry is the address the
       *caller claimed* and the right-most is the hop closest to us. Reading
       entry 0 -- which this function used to do -- means every rate limit and
       every per-address registration cap is bypassed by sending one header.
       Entries that can only be an internal hop are skipped, so a chain like
       `1.2.3.4, 10.0.0.1` still resolves to `1.2.3.4`.
    3. **`unknown`**, one shared bucket, when neither answers. Bucketing
       unknowns apart would be the same as not limiting them.

    Still not a proof of identity: an attacker upstream of Vercel can put
    anything in XFF, and header 1 is only as good as the platform. It is good
    enough for a bucket key, which is why the durable per-day caps in
    `oauthstore` are counted on top of this rather than instead of it.
    """
    real = usable_address(headers.get("x-real-ip") or "")
    if real:
        return real[:64]
    chain = (headers.get("x-forwarded-for") or "").split(",")
    for entry in reversed(chain):
        candidate = usable_address(entry)
        if candidate:
            return candidate[:64]
    return UNKNOWN_IP
