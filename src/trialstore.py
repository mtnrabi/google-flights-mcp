"""
Where the keyless allowance is counted.

One row per Google account per UTC day, in the same Neon database the stored
keys live in (`keystore.py`). Deliberately the same shape as that module --
Null / Memory / Postgres, asyncpg imported lazily inside the first call, one
connection per operation -- so there is one storage pattern in this package
rather than two.

Two rules this file exists to enforce:

* **The counter is per account.** `PRIMARY KEY (provider, user_sub, day)`.
  A global counter would let the first caller of the day spend everybody
  else's allowance, which is the failure the free server's daily budget has
  and the reason it needed a per-client cap on top of it.
* **The allowance is RESERVED before the searches run, in one statement, with
  the cap checked inside it.** Vercel runs many instances and a user can have
  two tool calls in flight at once. Reading the counter, running the searches
  and adding the cost afterwards means both callers read 0 and the day ends at
  twice the cap -- the counter would be a report, not a limit. So `reserve()`
  is a single `INSERT ... ON CONFLICT DO UPDATE ... WHERE
  searches + n <= cap RETURNING searches`: the `WHERE` on the `DO UPDATE`
  makes the statement write nothing and return no row when the reservation
  would breach the cap, so two racers cannot both win and a loser leaves no
  trace to clean up.
* **What was reserved and not used is refunded.** A plan of ten combinations
  reserves ten; if three fail before reaching the upstream, `refund()` gives
  three back. The order is deliberate: reserving first can only ever
  under-serve a caller (never overspend our key), and the refund closes that
  gap immediately afterwards rather than leaving it until midnight.

A store that is unavailable (no DATABASE_URL) or that fails is NOT treated as
"allowance available": the allowance spends our money, and a counter that
cannot count is a counter that cannot stop. The call site falls back to asking
the user for their own key -- see `server._trial_state`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

logger = logging.getLogger(__name__)

#: Same vocabulary as keystore.PROVIDER_GOOGLE; repeated rather than imported
#: so this module does not drag the crypto import in behind it.
PROVIDER_GOOGLE = "google"


class TrialStoreError(RuntimeError):
    """The allowance could not be read or recorded."""


class TrialStoreUnavailable(TrialStoreError):
    """No database configured on this deployment."""


@dataclass(frozen=True)
class TrialUsage:
    """What one account has spent on one day."""

    user_sub: str
    day: date
    searches: int


class TrialStore(Protocol):
    available: bool

    async def usage(
        self, user_sub: str, day: date, provider: str = PROVIDER_GOOGLE
    ) -> int: ...

    async def spend(
        self,
        user_sub: str,
        day: date,
        searches: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int: ...

    async def reserve(
        self,
        user_sub: str,
        day: date,
        searches: int,
        day_cap: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int | None: ...

    async def refund(
        self,
        user_sub: str,
        day: date,
        searches: int,
        provider: str = PROVIDER_GOOGLE,
    ) -> int: ...


class NullTrialStore:
    """No database: the allowance is off. Reads raise rather than return 0.

    Returning 0 would read as "nothing spent yet", which is the one answer
    that must never be guessed -- it would hand every caller an uncapped run
    on our key.
    """

    available = False

    async def usage(
        self, user_sub: str, day: date, provider: str = PROVIDER_GOOGLE
    ) -> int:
        raise TrialStoreUnavailable("no trial store configured (DATABASE_URL)")

    async def spend(
        self,
        user_sub: str,
        day: date,
        searches: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        raise TrialStoreUnavailable("no trial store configured (DATABASE_URL)")

    async def reserve(
        self,
        user_sub: str,
        day: date,
        searches: int,
        day_cap: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int | None:
        raise TrialStoreUnavailable("no trial store configured (DATABASE_URL)")

    async def refund(
        self,
        user_sub: str,
        day: date,
        searches: int,
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        raise TrialStoreUnavailable("no trial store configured (DATABASE_URL)")


class MemoryTrialStore:
    """In-process, for tests and for `python -m src` on a laptop."""

    available = True

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, date], int] = {}
        self._emails: dict[tuple[str, str], str] = {}

    async def usage(
        self, user_sub: str, day: date, provider: str = PROVIDER_GOOGLE
    ) -> int:
        return self._rows.get((provider, user_sub, day), 0)

    async def spend(
        self,
        user_sub: str,
        day: date,
        searches: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        key = (provider, user_sub, day)
        self._rows[key] = self._rows.get(key, 0) + max(0, searches)
        if email:
            self._emails[(provider, user_sub)] = email
        return self._rows[key]

    async def reserve(
        self,
        user_sub: str,
        day: date,
        searches: int,
        day_cap: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int | None:
        # No `await` between the read and the write, which is what makes this
        # the same atomic check-and-set the SQL performs: two coroutines racing
        # on one event loop cannot interleave inside it, so exactly one of them
        # can be the winner -- the property tests/test_trial.py asserts.
        searches = max(0, searches)
        if day_cap <= 0 or searches > day_cap:
            return None
        key = (provider, user_sub, day)
        current = self._rows.get(key, 0)
        if current + searches > day_cap:
            return None
        self._rows[key] = current + searches
        if email:
            self._emails[(provider, user_sub)] = email
        return self._rows[key]

    async def refund(
        self,
        user_sub: str,
        day: date,
        searches: int,
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        key = (provider, user_sub, day)
        self._rows[key] = max(0, self._rows.get(key, 0) - max(0, searches))
        return self._rows[key]


_SELECT = """
    SELECT searches
      FROM paid_trial_usage
     WHERE provider = $1 AND user_sub = $2 AND day = $3
"""

# One statement, so concurrent searches in one fan-out cannot lose an
# increment, and the new total comes back in the same round trip.
_SPEND = """
    INSERT INTO paid_trial_usage
        (provider, user_sub, day, searches, email, first_seen, updated_at)
    VALUES ($1, $2, $3, $4, NULLIF($5, ''), now(), now())
    ON CONFLICT (provider, user_sub, day) DO UPDATE SET
        searches   = paid_trial_usage.searches + EXCLUDED.searches,
        email      = COALESCE(EXCLUDED.email, paid_trial_usage.email),
        updated_at = now()
    RETURNING searches
"""


# The reservation, and the reason this whole module exists in the shape it
# does. ONE statement:
#
#   * no row yet  -> the INSERT branch writes `searches`, which the caller has
#     already been refused for if it exceeds the cap on its own (see the guard
#     in `reserve`); an `ON CONFLICT ... WHERE` does not constrain the INSERT
#     branch, so that check cannot live here.
#   * row exists  -> the DO UPDATE branch runs ONLY while the new total would
#     still fit under the cap. When it would not, the statement matches no row,
#     writes nothing and RETURNS NOTHING -- which is how a caller is refused
#     without a compensating write to undo.
#
# Two concurrent reservations therefore serialise on the row: the second sees
# the first's value, and at most one of them can come back with a number.
_RESERVE = """
    INSERT INTO paid_trial_usage
        (provider, user_sub, day, searches, email, first_seen, updated_at)
    VALUES ($1, $2, $3, $4, NULLIF($5, ''), now(), now())
    ON CONFLICT (provider, user_sub, day) DO UPDATE SET
        searches   = paid_trial_usage.searches + EXCLUDED.searches,
        email      = COALESCE(EXCLUDED.email, paid_trial_usage.email),
        updated_at = now()
     WHERE paid_trial_usage.searches + EXCLUDED.searches <= $6
    RETURNING searches
"""

# Giving back what was reserved and not spent. GREATEST(0, ...) rather than a
# bare subtraction: a refund that ran twice (a retried invocation, a duplicated
# settle) must not be able to hand a caller a negative counter and with it an
# allowance larger than the cap.
_REFUND = """
    UPDATE paid_trial_usage
       SET searches = GREATEST(0, searches - $4), updated_at = now()
     WHERE provider = $1 AND user_sub = $2 AND day = $3
    RETURNING searches
"""


class PostgresTrialStore:
    """Neon Postgres over asyncpg, one connection per operation.

    asyncpg is imported inside `_connect`, not at module import: most
    invocations on this server are keyed callers who never touch the
    allowance, and the import is pure cold-start cost for them. It is also the
    trap that took the free server down for 25 minutes on 2026-09-09 -- a
    module copied across without its requirement -- so note that asyncpg is
    already in this package's requirements.txt for the key store.
    """

    available = True

    def __init__(self, dsn: str, connect_timeout: float = 8.0) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    async def _connect(self):
        import asyncpg  # noqa: PLC0415 -- deliberately lazy, see class docstring

        return await asyncpg.connect(self._dsn, timeout=self._connect_timeout)

    async def usage(
        self, user_sub: str, day: date, provider: str = PROVIDER_GOOGLE
    ) -> int:
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001 - every driver error means the same thing here
            raise TrialStoreError(f"trial store unreachable: {exc}") from exc
        try:
            value = await conn.fetchval(_SELECT, provider, user_sub, day)
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial usage read failed: {exc}") from exc
        finally:
            await conn.close()
        return int(value or 0)

    async def spend(
        self,
        user_sub: str,
        day: date,
        searches: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial store unreachable: {exc}") from exc
        try:
            value = await conn.fetchval(
                _SPEND, provider, user_sub, day, max(0, searches), email or ""
            )
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial spend failed: {exc}") from exc
        finally:
            await conn.close()
        return int(value or 0)

    async def reserve(
        self,
        user_sub: str,
        day: date,
        searches: int,
        day_cap: int,
        email: str = "",
        provider: str = PROVIDER_GOOGLE,
    ) -> int | None:
        searches = max(0, searches)
        # The INSERT branch of `_RESERVE` is not covered by its WHERE, so a
        # first-of-the-day reservation bigger than the whole cap is refused
        # here. The caller never asks for one (the plan is capped by
        # `remaining` first); this is the belt.
        if day_cap <= 0 or searches > day_cap:
            return None
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial store unreachable: {exc}") from exc
        try:
            value = await conn.fetchval(
                _RESERVE,
                provider,
                user_sub,
                day,
                searches,
                email or "",
                day_cap,
            )
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial reserve failed: {exc}") from exc
        finally:
            await conn.close()
        # No row means the cap would have been breached. Nothing was written.
        return None if value is None else int(value)

    async def refund(
        self,
        user_sub: str,
        day: date,
        searches: int,
        provider: str = PROVIDER_GOOGLE,
    ) -> int:
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial store unreachable: {exc}") from exc
        try:
            value = await conn.fetchval(
                _REFUND, provider, user_sub, day, max(0, searches)
            )
        except Exception as exc:  # noqa: BLE001
            raise TrialStoreError(f"trial refund failed: {exc}") from exc
        finally:
            await conn.close()
        return int(value or 0)


def build_trial_store(dsn: str | None = None) -> TrialStore:
    """The store this deployment should use, or a NullTrialStore.

    Never raises for a missing configuration: an unconfigured deployment is
    the normal state until ops sets DATABASE_URL, and it must boot and serve
    keyed callers exactly as it does today.
    """
    import os as _os

    dsn = (dsn if dsn is not None else _os.environ.get("DATABASE_URL", "")).strip()
    if not dsn:
        return NullTrialStore()
    return PostgresTrialStore(dsn)
