"""
Call logging and counters.

One job, not two. The free server's telemetry also arms a spend guard, because
there the fan-out is our money. Here the caller pays their own RapidAPI bill,
so there is no budget of ours to defend and nothing in this module gates a
request. What is left is the honest-numbers job: every tool call emits exactly
one JSON line to stdout prefixed `MCP_CALL `, which works everywhere --
container, Vercel, or local -- and Vercel captures stdout automatically.

No field here records anything about the caller's identity, key, or IP. The
key never enters this module at all. That is not an oversight to be filled in
later: this server is built to be listed in directories whose review asks what
you collect, and "a call count and a duration" is a much better answer than
one that needs a privacy policy to explain.

Vercel note: runtime logs cap at 256 lines and 1 MB per request, so this emits
one line per tool call, never one per upstream request.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from .stores import CounterStore, MemoryCounterStore

logger = logging.getLogger(__name__)

STDOUT_PREFIX = "MCP_CALL "


@dataclass
class CallRecord:
    """One tool call, as logged."""

    timestamp: float
    tool: str
    requested_combinations: int
    upstream_calls: int
    upstream_failures: int
    results_returned: int
    duration_ms: int
    truncated: bool
    # Which mechanism supplied the key (header / query / env / none). The key
    # itself is never recorded -- this is here because "users cannot work out
    # how to pass a key" and "users are not trying" look identical without it.
    credential_source: str
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": round(self.timestamp, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "tool": self.tool,
            "requested_combinations": self.requested_combinations,
            # Billed RapidAPI requests for this call. Sum this field across
            # MCP_CALL lines to get total upstream requests for any period.
            "upstream_calls": self.upstream_calls,
            "upstream_failures": self.upstream_failures,
            "results_returned": self.results_returned,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "credential_source": self.credential_source,
            "error": self.error,
        }

    def counter_fields(self) -> dict[str, int]:
        return {
            "tool_calls": 1,
            "upstream_calls": self.upstream_calls,
            "upstream_failures": self.upstream_failures,
            "results_returned": self.results_returned,
            "truncated_calls": 1 if self.truncated else 0,
            "errored_calls": 1 if self.error else 0,
            "unauthenticated_calls": 1 if self.credential_source == "none" else 0,
        }


class Telemetry:
    def __init__(
        self,
        store: CounterStore | None = None,
        log_path: str | None = None,
        stdout: bool = True,
    ) -> None:
        self._store: CounterStore = store or MemoryCounterStore()
        self._stdout = stdout
        self._started_at = time.time()
        self._log_path = self._prepare_file_sink(log_path)

    # ── sinks ────────────────────────────────────────────────────────────

    def _prepare_file_sink(self, log_path: str | None) -> str | None:
        """Enable the file sink only if the path is genuinely writable.

        On Vercel everything outside /tmp is read-only, and /tmp itself has no
        durability guarantee. Rather than append into a void, the file sink
        turns itself off and stdout carries the record.
        """
        if not log_path:
            return None
        try:
            directory = os.path.dirname(os.path.abspath(log_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(log_path, "a", encoding="utf-8"):
                pass
            return log_path
        except OSError as exc:
            logger.info(
                "file log sink disabled (%s is not writable: %s); "
                "stdout MCP_CALL lines remain the record",
                log_path,
                exc,
            )
            return None

    @property
    def file_sink_enabled(self) -> bool:
        return self._log_path is not None

    @property
    def durable_counters(self) -> bool:
        return getattr(self._store, "durable", False)

    def _emit(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        if self._stdout:
            # stdout, not the logging module: MCP over stdio would own stdout,
            # but this server only ever runs over HTTP. Vercel maps stdout to
            # info-level runtime logs.
            print(STDOUT_PREFIX + line, file=sys.stdout, flush=True)
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                logger.warning("could not append to %s: %s", self._log_path, exc)

    # ── recording ────────────────────────────────────────────────────────

    async def record(self, record: CallRecord) -> None:
        self._emit(record.to_json())
        try:
            await self._store.bump(
                record.counter_fields(), record.upstream_calls, record.timestamp
            )
        except Exception as exc:  # noqa: BLE001 - counters are not the product
            logger.warning("counter update failed: %s", exc)

    # ── reporting ────────────────────────────────────────────────────────

    async def call_series(self, hours: int = 24) -> dict[str, Any]:
        """Per-hour call counts over the last `hours`, oldest first.

        Only meaningful with a durable store: per-instance counters reset on
        every cold start, so `durable` is reported alongside the data rather
        than leaving a caller to assume the zeros are real.
        """
        now = time.time()
        try:
            buckets = await self._store.call_series(now, hours)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read call series: %s", exc)
            buckets = []

        return {
            "hours": hours,
            "from": time.strftime(
                "%Y-%m-%dT%H:00:00Z", time.gmtime(now - (hours - 1) * 3600)
            ),
            "to": time.strftime("%Y-%m-%dT%H:59:59Z", time.gmtime(now)),
            "totals": {
                "tool_calls": sum(b["tool_calls"] for b in buckets),
                "upstream_calls": sum(b["upstream_calls"] for b in buckets),
            },
            "buckets": buckets,
            "durable": self.durable_counters,
            "note": (
                "upstream_calls is the billed RapidAPI request count; "
                "tool_calls is MCP tool invocations. Buckets are UTC hours, "
                "oldest first."
                if self.durable_counters
                else "NOT DURABLE - no shared store configured, so these counts "
                "cover only the process that answered this request and reset "
                "on every cold start. Configure UPSTASH_REDIS_REST_URL / _TOKEN."
            ),
        }

    async def snapshot(self) -> dict[str, Any]:
        try:
            counters = await self._store.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read counters: %s", exc)
            counters = {"totals": {}}

        totals = counters.get("totals", {})
        tool_calls = totals.get("tool_calls", 0)
        upstream_calls = totals.get("upstream_calls", 0)
        ratio = round(upstream_calls / tool_calls, 2) if tool_calls else 0.0

        notes = [
            "upstream_calls is the number of RapidAPI requests billed to "
            "callers' own subscriptions. This server never pays for a search.",
            "upstream_calls_per_tool_call is how many billed requests one user "
            "question costs. Lower is better for the user's plan quota, which "
            "is what makes them stay subscribed.",
        ]
        if not self.durable_counters:
            notes.append(
                "COUNTERS ARE NOT DURABLE: no shared store is configured, so "
                "these totals cover this process only and reset when it "
                "recycles. On serverless the real numbers are higher than "
                "shown. Configure UPSTASH_REDIS_REST_URL / _TOKEN."
            )
        if getattr(self._store, "degraded", False):
            notes.append(
                "The counter store returned an error recently; totals may be "
                "short by the writes that failed."
            )

        return {
            "uptime_seconds": int(time.time() - self._started_at),
            "totals": totals,
            "upstream_calls_per_tool_call": ratio,
            "durable_counters": self.durable_counters,
            "file_sink_enabled": self.file_sink_enabled,
            "notes": notes,
        }
