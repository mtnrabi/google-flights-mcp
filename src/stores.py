"""
Counter storage, behind an interface, because the deployment target decides
which one is correct.

Running as a container, an in-process dict is fine: one process, one set of
counters. Running on Vercel, Fluid compute shares one instance across
concurrent invocations and scales instances up and down freely, so in-process
counters fragment and reset. They do not error -- they return a number smaller
than the truth, which is the least useful way for a counter to be wrong.

Nothing here gates a request. Unlike the free server, this one has no spend
guard to arm: the caller pays their own RapidAPI bill, so there is no budget
of ours to protect. These counters exist for one reason -- to be able to
publish a real usage number (MCP Growth Playbook, tactic 7) instead of a
rounded-up one.

Upstash is reached over its REST API rather than the Redis wire protocol on
purpose: serverless invocations are short-lived and a connection-pooling
client is the wrong shape for them, plus it keeps `redis` out of the
dependency list.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Hourly buckets are retained for 35 days, which covers a monthly look-back
# with room to spare.
RETENTION_HOURS = 35 * 24
BUCKET_TTL_SECONDS = RETENTION_HOURS * 3600
MAX_QUERY_HOURS = RETENTION_HOURS

# Distinct from the free server's prefix on purpose: the two deployments may
# end up pointed at the same Upstash database, and silently summing their
# counters together would corrupt both numbers.
KEY_PREFIX = "gfpaid"

# Per-hour counters. `u` = upstream (RapidAPI) requests, `t` = MCP tool calls.
FIELD_UPSTREAM = "u"
FIELD_TOOL = "t"

# Fields worth a running total.
TRACKED_FIELDS = (
    "tool_calls",
    "upstream_calls",
    "upstream_failures",
    "results_returned",
    "truncated_calls",
    "errored_calls",
    "unauthenticated_calls",
)


def _hour_bucket(ts: float) -> str:
    return time.strftime("%Y%m%d%H", time.gmtime(ts))


def _bucket_range(now: float, hours: int) -> list[str]:
    """Hour labels from oldest to newest, inclusive of the current hour."""
    hours = max(1, min(int(hours), MAX_QUERY_HOURS))
    return [_hour_bucket(now - h * 3600) for h in range(hours - 1, -1, -1)]


class CounterStore(Protocol):
    durable: bool

    async def bump(
        self, fields: dict[str, int], upstream_calls: int, ts: float
    ) -> None: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]: ...


class MemoryCounterStore:
    """In-process counters. Correct for a container, wrong for serverless."""

    durable = False

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        self._buckets: dict[str, dict[str, int]] = {}

    async def bump(
        self, fields: dict[str, int], upstream_calls: int, ts: float
    ) -> None:
        for key, value in fields.items():
            if not value:
                continue
            self._totals[key] = self._totals.get(key, 0) + value
        slot = _hour_bucket(ts)
        hour = self._buckets.setdefault(slot, {FIELD_UPSTREAM: 0, FIELD_TOOL: 0})
        hour[FIELD_UPSTREAM] += upstream_calls
        hour[FIELD_TOOL] += int(fields.get("tool_calls") or 0)
        self._evict(ts)

    def _evict(self, now: float) -> None:
        """Drop buckets past the retention horizon so the dict cannot grow
        without bound in a long-lived container."""
        oldest = _hour_bucket(now - RETENTION_HOURS * 3600)
        for slot in list(self._buckets):
            if slot < oldest:
                del self._buckets[slot]

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]:
        out = []
        for label in _bucket_range(now, hours):
            hour = self._buckets.get(label) or {}
            out.append(
                {
                    "hour": label,
                    "tool_calls": int(hour.get(FIELD_TOOL) or 0),
                    "upstream_calls": int(hour.get(FIELD_UPSTREAM) or 0),
                }
            )
        return out

    async def snapshot(self) -> dict[str, Any]:
        return {"totals": dict(self._totals)}


class RedisCounterStore:
    """Upstash Redis over its REST API. Shared across instances.

    Every failure is logged and swallowed. Losing a counter must never fail a
    user's flight search, and a store outage must not become a product outage.
    """

    durable = True

    def __init__(
        self, url: str, token: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._client = client
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """True once a call has failed, so /metrics can admit the numbers may
        be short rather than reporting them as authoritative."""
        return self._degraded

    async def _pipeline(self, commands: list[list[Any]]) -> list[Any] | None:
        if not commands:
            return []
        payload = [[str(part) for part in command] for command in commands]
        try:
            client = self._client or httpx.AsyncClient(timeout=5.0)
            response = await client.post(
                f"{self._url}/pipeline",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5.0,
            )
            if self._client is None:
                await client.aclose()
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            self._degraded = True
            logger.warning("counter store write failed (%s)", exc)
            return None

    async def bump(
        self, fields: dict[str, int], upstream_calls: int, ts: float
    ) -> None:
        commands: list[list[Any]] = []
        for key, value in fields.items():
            if not value:
                continue
            commands.append(["INCRBY", f"{KEY_PREFIX}:total:{key}", value])
        slot = _hour_bucket(ts)
        tool_calls = int(fields.get("tool_calls") or 0)
        for field, amount in (
            (FIELD_UPSTREAM, upstream_calls),
            (FIELD_TOOL, tool_calls),
        ):
            if not amount:
                continue
            key = f"{KEY_PREFIX}:h:{slot}:{field}"
            commands.append(["INCRBY", key, amount])
            commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])
        await self._pipeline(commands)

    async def _mget_ints(self, keys: list[str]) -> list[int]:
        """MGET in chunks. A 35-day query is 840 keys; one giant MGET is rude."""
        values: list[int] = []
        chunk_size = 200
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start : start + chunk_size]
            result = await self._pipeline([["MGET", *chunk]])
            raw: list[Any] = []
            if result:
                try:
                    raw = result[0].get("result") or []
                except (AttributeError, IndexError):
                    raw = []
            for index in range(len(chunk)):
                try:
                    values.append(int(raw[index]))
                except (IndexError, TypeError, ValueError):
                    values.append(0)
        return values

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]:
        labels = _bucket_range(now, hours)
        upstream = await self._mget_ints(
            [f"{KEY_PREFIX}:h:{s}:{FIELD_UPSTREAM}" for s in labels]
        )
        tool = await self._mget_ints(
            [f"{KEY_PREFIX}:h:{s}:{FIELD_TOOL}" for s in labels]
        )
        return [
            {"hour": label, "tool_calls": tool[i], "upstream_calls": upstream[i]}
            for i, label in enumerate(labels)
        ]

    async def snapshot(self) -> dict[str, Any]:
        values = await self._mget_ints(
            [f"{KEY_PREFIX}:total:{f}" for f in TRACKED_FIELDS]
        )
        totals = {
            field: value for field, value in zip(TRACKED_FIELDS, values) if value
        }
        return {"totals": totals}


def build_counter_store(client: httpx.AsyncClient | None = None) -> CounterStore:
    """Pick a store from the environment.

    Recognises both credential names: Vercel's Upstash marketplace integration
    injects UPSTASH_REDIS_REST_*, while stores migrated from the retired
    Vercel KV carry KV_REST_API_*.
    """
    url = (
        os.environ.get("UPSTASH_REDIS_REST_URL")
        or os.environ.get("KV_REST_API_URL")
        or ""
    ).strip().strip('"').strip("'")
    token = (
        os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        or os.environ.get("KV_REST_API_TOKEN")
        or ""
    ).strip().strip('"').strip("'")

    if url and token:
        logger.info("using Upstash Redis counter store (durable, shared)")
        return RedisCounterStore(url, token, client=client)

    logger.info("using in-process counter store (not shared between instances)")
    return MemoryCounterStore()
