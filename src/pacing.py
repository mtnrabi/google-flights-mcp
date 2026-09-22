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
reason a month-long search is still fast: a bucket that starts full lets 93
requests go at once and only then makes the caller wait, so a search that
fits inside a minute's allowance pays NOTHING for the gate, while a second
concurrent one queues instead of tripping the Hub. A fixed interval would
have put half a second between every pair of requests including the first
three of a tiny search.

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


class TokenBucket:
    """`capacity` requests available at once, refilling at `per_minute`."""

    __slots__ = ("rate", "capacity", "tokens", "updated", "touched")

    def __init__(self, per_minute: int) -> None:
        self.rate = max(0.0, per_minute / 60.0)
        self.capacity = float(max(0, per_minute))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.touched = self.updated

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


def bucket_for(api_key: str, per_minute: int) -> TokenBucket | None:
    """The bucket every fan-out on this key shares. None when pacing is off."""
    if per_minute <= 0 or not api_key:
        return None
    bucket_id = credential_bucket_id(api_key)
    bucket = _BUCKETS.get(bucket_id)
    if bucket is None or bucket.capacity != float(per_minute):
        # A changed limit rebuilds rather than adjusts: the setting only
        # moves on a redeploy, and a half-migrated bucket is harder to
        # reason about than a fresh one.
        bucket = TokenBucket(per_minute)
        _BUCKETS[bucket_id] = bucket
        if len(_BUCKETS) > MAX_BUCKETS:
            oldest = min(_BUCKETS, key=lambda k: _BUCKETS[k].touched)
            if oldest != bucket_id:
                _BUCKETS.pop(oldest, None)
    return bucket


def reset_buckets() -> None:
    """Drop every bucket. For tests; nothing in production calls it."""
    _BUCKETS.clear()
