"""
Durable storage for the MCP-protocol OAuth flow: clients, codes, tokens.

Why this file exists at all
---------------------------
Day 1 (`/connect`) needed no server-side state: the session cookie and the
`fpk_` connect token are both self-contained signed payloads, so any instance
can verify one without having seen it issued. MCP-protocol OAuth cannot work
that way, and the reason is worth writing down because it is the exact
blocker that kept the flow out of day 1:

* **Dynamically-registered clients must be remembered.** A client registers,
  then seconds later calls /authorize. On Vercel those are two different
  instances. `OAuthProxy`'s default client store is process memory, so the
  second instance answers "unknown client_id" -- correct code, wrong storage.
* **Authorization codes must be single-use.** "Single use" is a claim about a
  row being consumed, which needs somewhere that two concurrent requests
  agree about. A signed, self-contained code cannot be burned.
* **Access and refresh tokens must be revocable.** A signed token is valid
  until it expires, whatever we later decide about it. RFC 7009 revocation,
  and "Disconnect logs every client out", both need a row to delete.

So: Neon Postgres, the same database day 1 uses, reached the same way --
asyncpg imported lazily inside the first call that touches it, one connection
per operation, pointed at the POOLED endpoint. Tables in
`migrations/002_mcp_oauth.sql`.

What is stored, and what is not
-------------------------------
Codes, access tokens, refresh tokens and client secrets are stored as
**SHA-256 hashes, never in the clear**. A database dump therefore contains
nothing that can be replayed. Plain SHA-256 rather than an HMAC under a
pepper because every one of these values is 32 bytes from `os.urandom`: there
is no dictionary to attack and a pepper would only add a second secret to
rotate.

A row holds the Google `sub` that authorised it -- the same identity day 1's
`mcp_user_keys` is keyed by, which is what lets an OAuth-authenticated tool
call find the user's stored RapidAPI key without a second sign-in. It never
holds a RapidAPI key; that stays in `mcp_user_keys`, encrypted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Bumped if the table shapes change in a way old rows cannot satisfy.
OAUTH_SCHEMA_VERSION = 1


class OAuthStoreError(RuntimeError):
    """Storage failed. Never reported to a client as "your request is bad"."""


def hash_secret(value: str) -> str:
    """The stored form of a code, a token or a client secret.

    Hex SHA-256. Constant across processes, so a token minted on one Vercel
    instance is recognised on another -- which is the entire point of this
    module.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


# ── records ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthClient:
    """One dynamically-registered MCP client.

    `client_secret_hash` is "" for a public client (`token_endpoint_auth_method
    = none`), which is what almost every MCP client registers as: they run on
    a user's machine and have nowhere to keep a secret. PKCE is what protects
    those, and this server requires PKCE from everybody, confidential clients
    included.
    """

    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    scope: str = ""
    client_secret_hash: str = ""
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_public(self) -> bool:
        return not self.client_secret_hash


@dataclass(frozen=True)
class AuthCode:
    """One issued authorization code, before it is exchanged.

    `code_challenge` is the S256 challenge the client sent; the verifier is
    never stored, because storing it would defeat the point of PKCE.
    """

    code_hash: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    user_sub: str
    provider: str
    resource: str
    expires_at: float


@dataclass(frozen=True)
class TokenRecord:
    """One access or refresh token."""

    token_hash: str
    kind: str  # "access" | "refresh"
    client_id: str
    user_sub: str
    provider: str
    scope: str
    resource: str
    expires_at: float


# ── the interface ────────────────────────────────────────────────────────


class OAuthStore(Protocol):
    available: bool

    async def register_client(self, client: OAuthClient) -> None: ...

    async def get_client(self, client_id: str) -> OAuthClient | None: ...

    async def put_code(self, code: AuthCode) -> None: ...

    async def consume_code(self, code_hash: str) -> AuthCode | None: ...

    async def put_token(self, token: TokenRecord) -> None: ...

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None: ...

    async def revoke_token(self, token_hash: str) -> bool: ...

    async def revoke_for_user(self, user_sub: str, provider: str) -> int: ...

    async def purge_expired(self, now: float | None = None) -> int: ...


class NullOAuthStore:
    """What an unconfigured deployment gets.

    Reads answer "nothing here"; writes raise. Same split as
    `keystore.NullKeyStore`, and for the same reason: a read that fails open
    is just an unauthenticated request, while a write that fails silently is
    a client told it registered when it did not.
    """

    available = False

    async def register_client(self, client: OAuthClient) -> None:
        raise OAuthStoreError("no OAuth store configured (DATABASE_URL)")

    async def get_client(self, client_id: str) -> OAuthClient | None:
        return None

    async def put_code(self, code: AuthCode) -> None:
        raise OAuthStoreError("no OAuth store configured (DATABASE_URL)")

    async def consume_code(self, code_hash: str) -> AuthCode | None:
        return None

    async def put_token(self, token: TokenRecord) -> None:
        raise OAuthStoreError("no OAuth store configured (DATABASE_URL)")

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None:
        return None

    async def revoke_token(self, token_hash: str) -> bool:
        return False

    async def revoke_for_user(self, user_sub: str, provider: str) -> int:
        return 0

    async def purge_expired(self, now: float | None = None) -> int:
        return 0


class MemoryOAuthStore:
    """In-process, for tests and `python -m src` on a laptop.

    Deliberately implements the same single-use and expiry semantics as the
    Postgres store rather than approximating them: a test that passes here
    and fails in production is worse than no test.
    """

    available = True

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._codes: dict[str, AuthCode] = {}
        self._tokens: dict[str, TokenRecord] = {}

    async def register_client(self, client: OAuthClient) -> None:
        self._clients[client.client_id] = client

    async def get_client(self, client_id: str) -> OAuthClient | None:
        return self._clients.get(client_id)

    async def put_code(self, code: AuthCode) -> None:
        self._codes[code.code_hash] = code

    async def consume_code(self, code_hash: str) -> AuthCode | None:
        # pop, not get: the row is gone whether or not the caller goes on to
        # accept it, so a replay of the same code finds nothing.
        return self._codes.pop(code_hash, None)

    async def put_token(self, token: TokenRecord) -> None:
        self._tokens[token.token_hash] = token

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None:
        record = self._tokens.get(token_hash)
        if record is None or record.kind != kind:
            return None
        if (now if now is not None else time.time()) >= record.expires_at:
            return None
        return record

    async def revoke_token(self, token_hash: str) -> bool:
        return self._tokens.pop(token_hash, None) is not None

    async def revoke_for_user(self, user_sub: str, provider: str) -> int:
        doomed = [
            h
            for h, t in self._tokens.items()
            if t.user_sub == user_sub and t.provider == provider
        ]
        for h in doomed:
            del self._tokens[h]
        return len(doomed)

    async def purge_expired(self, now: float | None = None) -> int:
        cutoff = now if now is not None else time.time()
        doomed = [h for h, t in self._tokens.items() if t.expires_at < cutoff]
        for h in doomed:
            del self._tokens[h]
        codes = [h for h, c in self._codes.items() if c.expires_at < cutoff]
        for h in codes:
            del self._codes[h]
        return len(doomed) + len(codes)


# ── Postgres ─────────────────────────────────────────────────────────────

_INSERT_CLIENT = """
INSERT INTO mcp_oauth_clients
       (client_id, client_name, redirect_uris, token_endpoint_auth_method,
        scope, client_secret_hash, metadata)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_SELECT_CLIENT = """
SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method,
       scope, client_secret_hash, created_at, metadata
  FROM mcp_oauth_clients
 WHERE client_id = $1
"""

_INSERT_CODE = """
INSERT INTO mcp_oauth_codes
       (code_hash, client_id, redirect_uri, code_challenge, scope,
        user_sub, provider, resource, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

#: DELETE ... RETURNING is the single-use guarantee. Two concurrent exchanges
#: of the same code race on one row and exactly one of them gets a result --
#: which is the property RFC 6749 §4.1.2 asks for and a SELECT-then-DELETE
#: does not have.
_CONSUME_CODE = """
DELETE FROM mcp_oauth_codes
 WHERE code_hash = $1
RETURNING code_hash, client_id, redirect_uri, code_challenge, scope,
          user_sub, provider, resource, expires_at
"""

_INSERT_TOKEN = """
INSERT INTO mcp_oauth_tokens
       (token_hash, kind, client_id, user_sub, provider, scope, resource,
        expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (token_hash) DO NOTHING
"""

_SELECT_TOKEN = """
SELECT token_hash, kind, client_id, user_sub, provider, scope, resource,
       expires_at
  FROM mcp_oauth_tokens
 WHERE token_hash = $1 AND kind = $2 AND revoked_at IS NULL
"""

_REVOKE_TOKEN = "DELETE FROM mcp_oauth_tokens WHERE token_hash = $1"

_REVOKE_USER = (
    "DELETE FROM mcp_oauth_tokens WHERE user_sub = $1 AND provider = $2"
)

_PURGE = """
WITH t AS (DELETE FROM mcp_oauth_tokens WHERE expires_at < $1 RETURNING 1),
     c AS (DELETE FROM mcp_oauth_codes  WHERE expires_at < $1 RETURNING 1)
SELECT (SELECT count(*) FROM t) + (SELECT count(*) FROM c)
"""


class PostgresOAuthStore:
    """Neon over asyncpg, one connection per operation.

    Same shape as `keystore.PostgresKeyStore` and for the same reasons: a
    serverless invocation is short-lived, and a pool that outlives one is a
    pool of sockets nobody closes. Point DATABASE_URL at the `-pooler` host.
    """

    available = True

    def __init__(self, dsn: str, connect_timeout: float = 8.0) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    async def _connect(self):
        import asyncpg  # noqa: PLC0415 -- lazy on purpose, see class docstring

        return await asyncpg.connect(self._dsn, timeout=self._connect_timeout)

    async def _run(self, fn):
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001 - asyncpg raises many shapes
            raise OAuthStoreError(f"could not reach the OAuth store: {exc}") from exc
        try:
            return await fn(conn)
        except OAuthStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OAuthStoreError(f"OAuth store operation failed: {exc}") from exc
        finally:
            await conn.close()

    # ── clients ──────────────────────────────────────────────────────────

    async def register_client(self, client: OAuthClient) -> None:
        import json  # noqa: PLC0415

        async def go(conn):
            await conn.execute(
                _INSERT_CLIENT,
                client.client_id,
                client.client_name,
                list(client.redirect_uris),
                client.token_endpoint_auth_method,
                client.scope,
                client.client_secret_hash,
                json.dumps(client.metadata or {}),
            )

        await self._run(go)

    async def get_client(self, client_id: str) -> OAuthClient | None:
        import json  # noqa: PLC0415

        async def go(conn):
            return await conn.fetchrow(_SELECT_CLIENT, client_id)

        row = await self._run(go)
        if row is None:
            return None
        raw_meta = row["metadata"]
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except ValueError:
                raw_meta = {}
        return OAuthClient(
            client_id=row["client_id"],
            client_name=row["client_name"],
            redirect_uris=tuple(row["redirect_uris"] or ()),
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
            scope=row["scope"] or "",
            client_secret_hash=row["client_secret_hash"] or "",
            created_at=_epoch(row["created_at"]) if row["created_at"] else 0.0,
            metadata=raw_meta if isinstance(raw_meta, dict) else {},
        )

    # ── codes ────────────────────────────────────────────────────────────

    async def put_code(self, code: AuthCode) -> None:
        async def go(conn):
            await conn.execute(
                _INSERT_CODE,
                code.code_hash,
                code.client_id,
                code.redirect_uri,
                code.code_challenge,
                code.scope,
                code.user_sub,
                code.provider,
                code.resource,
                _dt(code.expires_at),
            )

        await self._run(go)

    async def consume_code(self, code_hash: str) -> AuthCode | None:
        async def go(conn):
            return await conn.fetchrow(_CONSUME_CODE, code_hash)

        row = await self._run(go)
        if row is None:
            return None
        return AuthCode(
            code_hash=row["code_hash"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            scope=row["scope"] or "",
            user_sub=row["user_sub"],
            provider=row["provider"],
            resource=row["resource"] or "",
            expires_at=_epoch(row["expires_at"]),
        )

    # ── tokens ───────────────────────────────────────────────────────────

    async def put_token(self, token: TokenRecord) -> None:
        async def go(conn):
            await conn.execute(
                _INSERT_TOKEN,
                token.token_hash,
                token.kind,
                token.client_id,
                token.user_sub,
                token.provider,
                token.scope,
                token.resource,
                _dt(token.expires_at),
            )

        await self._run(go)

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None:
        async def go(conn):
            return await conn.fetchrow(_SELECT_TOKEN, token_hash, kind)

        row = await self._run(go)
        if row is None:
            return None
        record = TokenRecord(
            token_hash=row["token_hash"],
            kind=row["kind"],
            client_id=row["client_id"],
            user_sub=row["user_sub"],
            provider=row["provider"],
            scope=row["scope"] or "",
            resource=row["resource"] or "",
            expires_at=_epoch(row["expires_at"]),
        )
        # Expiry is checked here rather than in SQL so the clock that decides
        # is the same one the tests can move.
        if (now if now is not None else time.time()) >= record.expires_at:
            return None
        return record

    async def revoke_token(self, token_hash: str) -> bool:
        async def go(conn):
            return await conn.execute(_REVOKE_TOKEN, token_hash)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def revoke_for_user(self, user_sub: str, provider: str) -> int:
        async def go(conn):
            return await conn.execute(_REVOKE_USER, user_sub, provider)

        status = await self._run(go)
        tail = str(status).rsplit(" ", 1)[-1].strip()
        return int(tail) if tail.isdigit() else 0

    async def purge_expired(self, now: float | None = None) -> int:
        cutoff = _dt(now if now is not None else time.time())

        async def go(conn):
            return await conn.fetchval(_PURGE, cutoff)

        return int(await self._run(go) or 0)


def build_oauth_store(dsn: str | None = None) -> OAuthStore:
    """The store this deployment should use, or a NullOAuthStore.

    Never raises for a missing DATABASE_URL: that is the normal state of
    every deployment until ops sets it, and the server must boot and serve
    keyed callers exactly as it does today.
    """
    dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
    if not dsn:
        return NullOAuthStore()
    return PostgresOAuthStore(dsn)
