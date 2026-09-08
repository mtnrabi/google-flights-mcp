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
#: 2 adds migration 003: `user_email` on codes and tokens, `family_id` and a
#: kept `revoked_at` on tokens, `registered_ip` and `last_authorized_at` on
#: clients. Every one of them has a default, so code at version 1 keeps
#: working against a version-2 table.
OAUTH_SCHEMA_VERSION = 2


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
    #: Who registered it, for the per-address daily cap. Spoofable (it is a
    #: forwarded header), which is why it is one of two caps and not the only
    #: one.
    registered_ip: str = ""
    #: True for a client resolved from a Client ID Metadata Document rather
    #: than read out of our own table. Nothing about it is stored, so it is
    #: never swept and never counted against a registration cap.
    ephemeral: bool = False

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
    #: Carried so a tool call made with the resulting token can say "you are
    #: signed in as ..." instead of asking a signed-in user to sign in.
    user_email: str = ""


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
    user_email: str = ""
    #: Every token descended from one authorization shares this id. Refresh
    #: rotation carries it forward, so presenting an already-rotated refresh
    #: token can revoke the whole line in one statement.
    family_id: str = ""
    #: Set when a refresh token is rotated out. The row is KEPT rather than
    #: deleted: a deleted row and a stolen-and-replayed one look identical,
    #: and telling them apart is the entire point of reuse detection.
    revoked_at: float | None = None


# ── the interface ────────────────────────────────────────────────────────


class OAuthStore(Protocol):
    available: bool

    async def register_client(self, client: OAuthClient) -> None: ...

    async def get_client(self, client_id: str) -> OAuthClient | None: ...

    async def count_clients_since(
        self, since: float, ip: str | None = None
    ) -> int: ...

    async def mark_client_authorized(
        self, client_id: str, now: float | None = None
    ) -> None: ...

    async def purge_stale_clients(self, cutoff: float) -> int: ...

    async def put_code(self, code: AuthCode) -> None: ...

    async def consume_code(self, code_hash: str) -> AuthCode | None: ...

    async def put_token(self, token: TokenRecord) -> None: ...

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None: ...

    async def get_token_any(self, token_hash: str, kind: str) -> TokenRecord | None: ...

    async def rotate_token(self, token_hash: str, now: float | None = None) -> bool: ...

    async def revoke_token(self, token_hash: str) -> bool: ...

    async def revoke_family(self, family_id: str) -> int: ...

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

    async def count_clients_since(self, since: float, ip: str | None = None) -> int:
        return 0

    async def mark_client_authorized(
        self, client_id: str, now: float | None = None
    ) -> None:
        return None

    async def purge_stale_clients(self, cutoff: float) -> int:
        return 0

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

    async def get_token_any(self, token_hash: str, kind: str) -> TokenRecord | None:
        return None

    async def rotate_token(self, token_hash: str, now: float | None = None) -> bool:
        return False

    async def revoke_token(self, token_hash: str) -> bool:
        return False

    async def revoke_family(self, family_id: str) -> int:
        return 0

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
        self._authorized: dict[str, float] = {}

    async def register_client(self, client: OAuthClient) -> None:
        self._clients[client.client_id] = client

    async def get_client(self, client_id: str) -> OAuthClient | None:
        return self._clients.get(client_id)

    async def count_clients_since(self, since: float, ip: str | None = None) -> int:
        return sum(
            1
            for c in self._clients.values()
            if c.created_at >= since and (ip is None or c.registered_ip == ip)
        )

    async def mark_client_authorized(
        self, client_id: str, now: float | None = None
    ) -> None:
        self._authorized[client_id] = now if now is not None else time.time()

    async def purge_stale_clients(self, cutoff: float) -> int:
        doomed = [
            cid
            for cid, c in self._clients.items()
            if c.created_at < cutoff
            and cid not in self._authorized
            and not any(t.client_id == cid for t in self._tokens.values())
            and not any(k.client_id == cid for k in self._codes.values())
        ]
        for cid in doomed:
            del self._clients[cid]
        return len(doomed)

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
        if record.revoked_at is not None:
            return None
        if (now if now is not None else time.time()) >= record.expires_at:
            return None
        return record

    async def get_token_any(self, token_hash: str, kind: str) -> TokenRecord | None:
        record = self._tokens.get(token_hash)
        if record is None or record.kind != kind:
            return None
        return record

    async def rotate_token(self, token_hash: str, now: float | None = None) -> bool:
        record = self._tokens.get(token_hash)
        if record is None:
            return False
        from dataclasses import replace  # noqa: PLC0415 -- one call site

        self._tokens[token_hash] = replace(
            record, revoked_at=now if now is not None else time.time()
        )
        return True

    async def revoke_token(self, token_hash: str) -> bool:
        return self._tokens.pop(token_hash, None) is not None

    async def revoke_family(self, family_id: str) -> int:
        if not family_id:
            return 0
        doomed = [h for h, t in self._tokens.items() if t.family_id == family_id]
        for h in doomed:
            del self._tokens[h]
        return len(doomed)

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
        scope, client_secret_hash, metadata, registered_ip)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_SELECT_CLIENT = """
SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method,
       scope, client_secret_hash, created_at, metadata, registered_ip
  FROM mcp_oauth_clients
 WHERE client_id = $1
"""

_COUNT_CLIENTS = """
SELECT count(*) FROM mcp_oauth_clients
 WHERE created_at >= $1
"""

_COUNT_CLIENTS_BY_IP = """
SELECT count(*) FROM mcp_oauth_clients
 WHERE created_at >= $1 AND registered_ip = $2
"""

_MARK_CLIENT_AUTHORIZED = """
UPDATE mcp_oauth_clients SET last_authorized_at = $2 WHERE client_id = $1
"""

#: A registration that never became an authorization is litter: anyone can
#: POST /oauth/register, and a client that has not been approved by a human
#: within a day is not going to be. The two NOT EXISTS clauses are what keeps
#: this safe for rows written before `last_authorized_at` existed -- a client
#: with a live token or an outstanding code is never swept, whatever the
#: column says.
_PURGE_STALE_CLIENTS = """
DELETE FROM mcp_oauth_clients c
 WHERE c.created_at < $1
   AND c.last_authorized_at IS NULL
   AND NOT EXISTS (
         SELECT 1 FROM mcp_oauth_tokens t WHERE t.client_id = c.client_id)
   AND NOT EXISTS (
         SELECT 1 FROM mcp_oauth_codes k WHERE k.client_id = c.client_id)
"""

_INSERT_CODE = """
INSERT INTO mcp_oauth_codes
       (code_hash, client_id, redirect_uri, code_challenge, scope,
        user_sub, provider, resource, expires_at, user_email)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""

#: DELETE ... RETURNING is the single-use guarantee. Two concurrent exchanges
#: of the same code race on one row and exactly one of them gets a result --
#: which is the property RFC 6749 §4.1.2 asks for and a SELECT-then-DELETE
#: does not have.
_CONSUME_CODE = """
DELETE FROM mcp_oauth_codes
 WHERE code_hash = $1
RETURNING code_hash, client_id, redirect_uri, code_challenge, scope,
          user_sub, provider, resource, expires_at, user_email
"""

_INSERT_TOKEN = """
INSERT INTO mcp_oauth_tokens
       (token_hash, kind, client_id, user_sub, provider, scope, resource,
        expires_at, user_email, family_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (token_hash) DO NOTHING
"""

_TOKEN_COLUMNS = """
       token_hash, kind, client_id, user_sub, provider, scope, resource,
       expires_at, user_email, family_id, revoked_at
"""

_SELECT_TOKEN = f"""
SELECT {_TOKEN_COLUMNS}
  FROM mcp_oauth_tokens
 WHERE token_hash = $1 AND kind = $2 AND revoked_at IS NULL
"""

#: Revoked rows included, deliberately: a refresh token that was rotated out
#: is exactly the row reuse detection needs to find.
_SELECT_TOKEN_ANY = f"""
SELECT {_TOKEN_COLUMNS}
  FROM mcp_oauth_tokens
 WHERE token_hash = $1 AND kind = $2
"""

_ROTATE_TOKEN = """
UPDATE mcp_oauth_tokens SET revoked_at = $2
 WHERE token_hash = $1 AND revoked_at IS NULL
"""

_REVOKE_TOKEN = "DELETE FROM mcp_oauth_tokens WHERE token_hash = $1"

_REVOKE_FAMILY = "DELETE FROM mcp_oauth_tokens WHERE family_id = $1"

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
                client.registered_ip,
            )

        await self._run(go)

    async def count_clients_since(self, since: float, ip: str | None = None) -> int:
        cutoff = _dt(since)

        async def go(conn):
            if ip is None:
                return await conn.fetchval(_COUNT_CLIENTS, cutoff)
            return await conn.fetchval(_COUNT_CLIENTS_BY_IP, cutoff, ip)

        return int(await self._run(go) or 0)

    async def mark_client_authorized(
        self, client_id: str, now: float | None = None
    ) -> None:
        stamp = _dt(now if now is not None else time.time())

        async def go(conn):
            await conn.execute(_MARK_CLIENT_AUTHORIZED, client_id, stamp)

        await self._run(go)

    async def purge_stale_clients(self, cutoff: float) -> int:
        when = _dt(cutoff)

        async def go(conn):
            return await conn.execute(_PURGE_STALE_CLIENTS, when)

        status = await self._run(go)
        tail = str(status).rsplit(" ", 1)[-1].strip()
        return int(tail) if tail.isdigit() else 0

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
            registered_ip=row["registered_ip"] or "",
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
                code.user_email,
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
            user_email=row["user_email"] or "",
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
                token.user_email,
                token.family_id,
            )

        await self._run(go)

    @staticmethod
    def _token_from(row) -> TokenRecord:
        return TokenRecord(
            token_hash=row["token_hash"],
            kind=row["kind"],
            client_id=row["client_id"],
            user_sub=row["user_sub"],
            provider=row["provider"],
            scope=row["scope"] or "",
            resource=row["resource"] or "",
            expires_at=_epoch(row["expires_at"]),
            user_email=row["user_email"] or "",
            family_id=row["family_id"] or "",
            revoked_at=(
                _epoch(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
        )

    async def get_token(
        self, token_hash: str, kind: str, now: float | None = None
    ) -> TokenRecord | None:
        async def go(conn):
            return await conn.fetchrow(_SELECT_TOKEN, token_hash, kind)

        row = await self._run(go)
        if row is None:
            return None
        record = self._token_from(row)
        # Expiry is checked here rather than in SQL so the clock that decides
        # is the same one the tests can move.
        if (now if now is not None else time.time()) >= record.expires_at:
            return None
        return record

    async def get_token_any(self, token_hash: str, kind: str) -> TokenRecord | None:
        async def go(conn):
            return await conn.fetchrow(_SELECT_TOKEN_ANY, token_hash, kind)

        row = await self._run(go)
        return None if row is None else self._token_from(row)

    async def rotate_token(self, token_hash: str, now: float | None = None) -> bool:
        stamp = _dt(now if now is not None else time.time())

        async def go(conn):
            return await conn.execute(_ROTATE_TOKEN, token_hash, stamp)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def revoke_token(self, token_hash: str) -> bool:
        async def go(conn):
            return await conn.execute(_REVOKE_TOKEN, token_hash)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def revoke_family(self, family_id: str) -> int:
        if not family_id:
            return 0

        async def go(conn):
            return await conn.execute(_REVOKE_FAMILY, family_id)

        status = await self._run(go)
        tail = str(status).rsplit(" ", 1)[-1].strip()
        return int(tail) if tail.isdigit() else 0

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
