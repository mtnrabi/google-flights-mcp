"""
One rate limit per KEY, shared by every fan-out running on it.

Why this module exists
----------------------
The listing's plans are rate limited per minute as well as per month (PRO
150/min, ULTRA 250, MEGA 500), and RapidAPI answers a burst past the limit
with 429 -- which `src/rapidapi_client.py` reads, correctly, as "plan
exhausted", because against RapidAPI that is usually what a 429 means. So a
caller with 45,000 requests left can be told their plan is spent purely
because we sent too many at once.

The first version of the pace lived inside `execute_plan` and spread the
starts of ONE fan-out. That is not the limit RapidAPI enforces. Two
concurrent whole-month searches on the same key -- one client, two tabs, or
a model that fires flights and hotels together -- are two fan-outs of 93,
each individually under the limit and 186 a minute between them. The bucket
has to be per key and outlive the call.

A token bucket rather than a fixed interval, and that choice is the whole
reason an ordinary search is still instant: a bucket that starts with a
burst of tokens lets a normal call go at once and only then makes the caller
wait, while a second concurrent fan-out queues instead of tripping the Hub. A
fixed interval would have put half a second between every pair of requests
including the first three of a tiny search.

CAPACITY AND REFILL ARE TWO NUMBERS, and that is the whole safety property
-------------------------------------------------------------------------
A bucket's worst rolling minute is `capacity + 60 x rate`, because it can
empty a full bucket instantly and then take everything the refill hands out.
With capacity tied to the refill (120 and 120) that worst minute was **240**,
above a PRO key's own 150/min -- so the gate that exists to prevent a 429
could cause one. Splitting them fixes it arithmetically rather than by
hoping:

    capacity 30 + refill 120/min  ->  worst rolling minute 30 + 120 = 150

which is exactly the PRO limit. 30 is `DEFAULT_MAX_SEARCHES` on purpose: an
ordinary call is capped at 30 combinations, so an ordinary call still empties
into a full bucket and waits for nothing at all. Only a search that went past
the default cap -- the whole month, the three-destination month -- pays the
refill, which is the traffic the limit is about. Cost: 279 combinations take
(279 - 30) / 2 = ~125s rather than ~80s, inside the 280s fan-out deadline with
room.

WHAT THE 150 COVERS, EXACTLY: **dispatches**, not HTTP requests. One token is
taken per combination, and `src/rapidapi_client.py` retries a 5xx once
(`_MAX_ATTEMPTS = 2`) INSIDE the call that already holds its token -- the retry
never asks this bucket for anything. So a minute in which every dispatched
search fails once can put up to 300 requests on the wire, and `api_usage.
hub_requests_billed` is the figure that counts them. The retry policy is
deliberate (a 5xx that is not retried is a fare the caller paid for and did not
get), so the honest statement is: at most 150 searches START in any rolling
minute, plus at most one extra attempt for each of them that fails.

No lock. The token arithmetic contains no `await`, so on a single-threaded
event loop it is already atomic, and an `asyncio.Lock` held in a
process-global registry is a real hazard on serverless: the lock binds to
the loop that first used it, and the next invocation on a warm instance can
be a different loop. `time.monotonic` is used for the same reason -- it does
not belong to a loop.

The key is never stored. The registry is keyed by a SHA-256 of the
credential, so a heap dump of this process cannot hand anyone a RapidAPI
key, and two callers with the same key correctly share one bucket.
"""

from __future__ import annotations

import hashlib
import time

#: How many buckets to keep. One per distinct key seen by this instance;
#: the eviction exists so a long-lived instance serving many keys cannot
#: grow without bound. Evicting a bucket loses its accumulated debt, which
#: is why the least RECENTLY used goes first -- an idle bucket has refilled
#: to full anyway, so dropping it costs nothing.
MAX_BUCKETS = 512

#: Tokens available at once, before the refill rate starts to bite. Kept
#: SEPARATE from the refill because the worst rolling minute is the sum of
#: the two: 30 + 120 = 150 DISPATCHES, exactly a PRO key's limit. It bounds
#: searches STARTED, not requests sent: a retried 5xx reuses the token its
#: call already took, so a failing minute can add up to one more attempt per
#: dispatch. See the module docstring.
#:
#: 30 is `settings.DEFAULT_MAX_SEARCHES`, written here as a literal so this
#: module keeps importing nothing but the standard library (settings imports
#: from src.trial, and a cycle through it would be a boot-order bug rather
#: than a tidy constant). The two are pinned equal by a test: an ordinary
#: call is capped at the default, so an ordinary call must never wait.
DEFAULT_HUB_BURST_CAPACITY = 30


class TokenBucket:
    """`capacity` requests available at once, refilling at `per_minute`.

    Two numbers, never one: the worst rolling minute this bucket can allow
    is `capacity + per_minute`, so tying them together doubles the limit
    the gate was configured with. `capacity` defaults to `per_minute` only
    to keep the one-argument form meaningful in a test; production always
    passes both (see `bucket_for` and DEFAULT_HUB_BURST_CAPACITY).
    """

    __slots__ = ("rate", "capacity", "tokens", "updated", "touched")

    def __init__(self, per_minute: int, capacity: int | None = None) -> None:
        self.rate = max(0.0, per_minute / 60.0)
        if capacity is None:
            capacity = per_minute
        # Never zero while the bucket is on: a capacity of 0 with a live
        # refill rate means the FIRST request of every call waits half a
        # second for a token that a burst of one would have covered.
        self.capacity = float(max(1, capacity)) if per_minute > 0 else 0.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.touched = self.updated

    @property
    def worst_minute(self) -> float:
        """Most requests this bucket can let through in any 60 seconds."""
        return self.capacity + self.rate * 60.0

    def take(self) -> float:
        """Take one token, or return how many seconds until one exists.

        Contains no `await` on purpose: see the module docstring.
        """
        if self.rate <= 0:
            return 0.0
        now = time.monotonic()
        self.touched = now
        self.tokens = min(
            self.capacity, self.tokens + (now - self.updated) * self.rate
        )
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


_BUCKETS: dict[str, TokenBucket] = {}


def credential_bucket_id(api_key: str) -> str:
    """A stable id for a key that is not the key.

    Truncated because the registry is a local dict, not a security boundary:
    16 bytes of SHA-256 is far past any chance of two live keys colliding.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def bucket_for(
    api_key: str, per_minute: int, capacity: int | None = None
) -> TokenBucket | None:
    """The bucket every fan-out on this key shares. None when pacing is off."""
    if per_minute <= 0 or not api_key:
        return None
    if capacity is None:
        capacity = DEFAULT_HUB_BURST_CAPACITY
    wanted = float(max(1, capacity))
    bucket_id = credential_bucket_id(api_key)
    bucket = _BUCKETS.get(bucket_id)
    if bucket is None or bucket.capacity != wanted or bucket.rate != per_minute / 60.0:
        # A changed limit rebuilds rather than adjusts: the setting only
        # moves on a redeploy, and a half-migrated bucket is harder to
        # reason about than a fresh one.
        bucket = TokenBucket(per_minute, capacity)
        _BUCKETS[bucket_id] = bucket
        if len(_BUCKETS) > MAX_BUCKETS:
            oldest = min(_BUCKETS, key=lambda k: _BUCKETS[k].touched)
            if oldest != bucket_id:
                _BUCKETS.pop(oldest, None)
    return bucket


def reset_buckets() -> None:
    """Drop every bucket. For tests; nothing in production calls it."""
    _BUCKETS.clear()
