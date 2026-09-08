"""
Where a signed-in user's RapidAPI key lives between their browser and their
MCP client.

Why this exists
---------------
Every search on this server is billed to the caller's own RapidAPI
subscription, so the key has to reach us on every request. Today the only
ways to do that are a header, a query parameter, or a gateway config blob
(see credentials.py). All three mean the user handles the raw key in their
MCP client, and two of the three put it in a URL -- which is exactly where a
secret should not be, because URLs end up in proxy logs, screen shares and
support tickets.

The alternative offered here: the user signs in with Google on /connect,
pastes the key ONCE into a real web page, and gets back a connect URL that
carries a revocable token instead of the key. The key itself is encrypted at
rest and decrypted only for the duration of one search.

Rules this file exists to enforce
---------------------------------
* **The plaintext key is never stored, never logged, and never returned to a
  browser.** `StoredKey.last4` is the only part of it that leaves this module
  in a readable form, and `redact` (credentials.py) is the only other thing
  allowed to describe a key in a log line.
* **A wrong or missing master key must fail closed**, not fall back to
  plaintext. AES-GCM authenticates the ciphertext, so a decrypt under the
  wrong key raises rather than returning garbage; `KeyDecryptionError` is
  what the call site sees, and it turns into "connect again", never into a
  search billed to somebody else.
* **A deployment with nothing configured behaves exactly as it does today.**
  `build_key_store()` returns a store whose `available` is False when
  DATABASE_URL or MCP_KEY_MASTER is missing, /connect is not registered, and
  no request path changes. That is what makes this change a no-op on the two
  live deployments until ops sets three env vars.

Storage
-------
Neon Postgres, the same database the backend's attribution rollup writes to
(`backend/src/configuration.py:database_url`). Reached with asyncpg, imported
lazily inside the first call so a cold start that never touches a stored key
never pays for the import. One connection per operation rather than a pool:
serverless invocations are short-lived, instances come and go, and a pool
that outlives an invocation on Vercel is a pool of sockets nobody closes.
Point DATABASE_URL at Neon's **pooled** endpoint (the `-pooler` host) for the
same reason.

Schema: migrations/001_mcp_user_keys.sql. `key_version` is there so a master
key can be rotated without a downtime window -- rows written under version N
stay readable while new rows are written under N+1.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Bumped when the encryption scheme or the master key changes. Stored on
#: every row so a rotation can be staged rather than flag-day'd.
KEY_VERSION = 1

#: AES-256-GCM. 32-byte key, 12-byte nonce (the size AES-GCM is specified
#: for; anything else forces an internal GHASH of the nonce and buys nothing).
MASTER_KEY_BYTES = 32
NONCE_BYTES = 12

#: The identity provider a `user_sub` came from. One column rather than an
#: assumption, because "sub" is only unique *within* an issuer -- a second
#: provider later must not be able to collide with a Google account.
PROVIDER_GOOGLE = "google"


class KeyStoreError(RuntimeError):
    """Base for every failure that must not be reported as a wrong key."""


class KeyStoreUnavailable(KeyStoreError):
    """No database or no master key configured on this deployment."""


class MasterKeyError(KeyStoreError):
    """MCP_KEY_MASTER is missing or is not 32 bytes of base64."""


class KeyDecryptionError(KeyStoreError):
    """The stored ciphertext did not authenticate under the master key.

    Raised for a rotated-away master key, a truncated row, or a tampered
    ciphertext -- and deliberately not distinguished between them, because
    the honest answer to the user is the same in all three: connect again.
    """


# ── crypto ───────────────────────────────────────────────────────────────


def load_master_key(raw: str | None = None) -> bytes:
    """Decode MCP_KEY_MASTER into 32 raw bytes, or raise.

    Standard *and* URL-safe base64 are accepted, padded or not: this value
    gets copied through shells, Vercel's env UI and password managers, and a
    key that is right but pasted in the other alphabet should not read as a
    configuration error nobody can see.
    """
    value = (raw if raw is not None else os.environ.get("MCP_KEY_MASTER", "")).strip()
    # Values copied out of a .env file often keep their quotes; the two env
    # files in this repo both quote (see settings._strip_quotes).
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not value:
        raise MasterKeyError("MCP_KEY_MASTER is not set")
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) == MASTER_KEY_BYTES:
            return decoded
    raise MasterKeyError(
        "MCP_KEY_MASTER must be base64 of exactly "
        f"{MASTER_KEY_BYTES} bytes; got {len(value)} base64 characters"
    )


def encrypt_key(plaintext: str, master: bytes) -> tuple[bytes, bytes]:
    """Encrypt one RapidAPI key. Returns (ciphertext, nonce).

    AES-256-GCM, a fresh random nonce per write. The nonce is stored beside
    the ciphertext because it is not a secret -- what it must never be is
    reused under the same key, which is why it is generated here and never
    passed in.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not plaintext:
        raise ValueError("refusing to encrypt an empty key")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(master).encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce


def decrypt_key(ciphertext: bytes, nonce: bytes, master: bytes) -> str:
    """The inverse. Raises KeyDecryptionError for anything that does not
    authenticate -- never returns a partially-decrypted value."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(master).decrypt(bytes(nonce), bytes(ciphertext), None).decode(
            "utf-8"
        )
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        # The message deliberately says nothing about the ciphertext, the
        # key, or which of the three causes applied.
        raise KeyDecryptionError("stored key could not be decrypted") from exc


def last4(key: str) -> str:
    """The only part of a key that is allowed to be shown to a human."""
    return key[-4:] if len(key) >= 4 else ""


# ── records ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredKey:
    """One user's connected RapidAPI key, already decrypted.

    Held for the length of one request and never logged. `key_last4` is what
    goes on a page; `key` is what goes upstream.
    """

    user_sub: str
    email: str
    provider: str
    key: str
    key_last4: str
    key_version: int


@dataclass(frozen=True)
class KeySummary:
    """What /connect is allowed to render: no ciphertext, no plaintext."""

    email: str
    key_last4: str
    key_version: int
    updated_at: Any = None


# ── stores ───────────────────────────────────────────────────────────────


class KeyStore(Protocol):
    available: bool

    async def get(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> StoredKey | None: ...

    async def summary(
        self, user_sub: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary | None: ...

    async def put(
        self, user_sub: str, email: str, key: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary: ...

    async def revoke(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> bool: ...


class NullKeyStore:
    """The store a deployment gets when nothing is configured.

    `get` returns None rather than raising, because a *read* on an
    unconfigured deployment is just "this caller has no stored key" and the
    request continues down the header/query path exactly as before. A `put`
    raises, because pretending to save someone's key and dropping it is the
    one failure mode a user cannot detect.
    """

    available = False

    async def get(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> StoredKey | None:
        return None

    async def summary(
        self, user_sub: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary | None:
        return None

    async def put(
        self, user_sub: str, email: str, key: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary:
        raise KeyStoreUnavailable(
            "no key store configured (DATABASE_URL and MCP_KEY_MASTER)"
        )

    async def revoke(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> bool:
        return False


class MemoryKeyStore:
    """In-process, for tests and for `python -m src` on a laptop.

    Encrypts anyway. Not for secrecy against someone with the process memory,
    but so the tests exercise the same encrypt/decrypt path production does --
    a store that skipped it would let a broken cipher pass the suite.
    """

    available = True

    def __init__(self, master: bytes) -> None:
        self._master = master
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def get(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> StoredKey | None:
        row = self._rows.get((provider, user_sub))
        if row is None or row.get("revoked_at") is not None:
            return None
        key = decrypt_key(row["key_ciphertext"], row["key_nonce"], self._master)
        return StoredKey(
            user_sub=user_sub,
            email=row["email"],
            provider=provider,
            key=key,
            key_last4=row["key_last4"],
            key_version=row["key_version"],
        )

    async def summary(
        self, user_sub: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary | None:
        row = self._rows.get((provider, user_sub))
        if row is None or row.get("revoked_at") is not None:
            return None
        return KeySummary(
            email=row["email"],
            key_last4=row["key_last4"],
            key_version=row["key_version"],
            updated_at=row.get("updated_at"),
        )

    async def put(
        self, user_sub: str, email: str, key: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary:
        ciphertext, nonce = encrypt_key(key, self._master)
        self._rows[(provider, user_sub)] = {
            "email": email,
            "key_ciphertext": ciphertext,
            "key_nonce": nonce,
            "key_last4": last4(key),
            "key_version": KEY_VERSION,
            "revoked_at": None,
            "updated_at": None,
        }
        return KeySummary(email=email, key_last4=last4(key), key_version=KEY_VERSION)

    async def revoke(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> bool:
        # DELETE, not a tombstone. "Disconnect" on the page says the key is
        # removed, and a row that still holds the ciphertext would make that
        # sentence false.
        return self._rows.pop((provider, user_sub), None) is not None


_SELECT = """
    SELECT email, key_ciphertext, key_nonce, key_last4, key_version, updated_at
      FROM mcp_user_keys
     WHERE provider = $1 AND user_sub = $2 AND revoked_at IS NULL
"""

_UPSERT = """
    INSERT INTO mcp_user_keys
        (provider, user_sub, email, key_ciphertext, key_nonce, key_last4,
         key_version, created_at, updated_at, revoked_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now(), NULL)
    ON CONFLICT (provider, user_sub) DO UPDATE SET
        email           = EXCLUDED.email,
        key_ciphertext  = EXCLUDED.key_ciphertext,
        key_nonce       = EXCLUDED.key_nonce,
        key_last4       = EXCLUDED.key_last4,
        key_version     = EXCLUDED.key_version,
        updated_at      = now(),
        revoked_at      = NULL
    RETURNING updated_at
"""

_DELETE = "DELETE FROM mcp_user_keys WHERE provider = $1 AND user_sub = $2"


class PostgresKeyStore:
    """Neon Postgres over asyncpg, one connection per operation.

    asyncpg is imported inside `_connect`, not at module import: the vast
    majority of invocations on this server never look at a stored key, and
    the import is pure cold-start cost for them.
    """

    available = True

    def __init__(self, dsn: str, master: bytes, connect_timeout: float = 8.0) -> None:
        self._dsn = dsn
        self._master = master
        self._connect_timeout = connect_timeout

    async def _connect(self):
        import asyncpg  # noqa: PLC0415 -- deliberately lazy, see class docstring

        return await asyncpg.connect(self._dsn, timeout=self._connect_timeout)

    async def get(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> StoredKey | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(_SELECT, provider, user_sub)
        finally:
            await conn.close()
        if row is None:
            return None
        key = decrypt_key(row["key_ciphertext"], row["key_nonce"], self._master)
        return StoredKey(
            user_sub=user_sub,
            email=row["email"],
            provider=provider,
            key=key,
            key_last4=row["key_last4"],
            key_version=row["key_version"],
        )

    async def summary(
        self, user_sub: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(_SELECT, provider, user_sub)
        finally:
            await conn.close()
        if row is None:
            return None
        return KeySummary(
            email=row["email"],
            key_last4=row["key_last4"],
            key_version=row["key_version"],
            updated_at=row["updated_at"],
        )

    async def put(
        self, user_sub: str, email: str, key: str, provider: str = PROVIDER_GOOGLE
    ) -> KeySummary:
        ciphertext, nonce = encrypt_key(key, self._master)
        conn = await self._connect()
        try:
            updated_at = await conn.fetchval(
                _UPSERT,
                provider,
                user_sub,
                email,
                ciphertext,
                nonce,
                last4(key),
                KEY_VERSION,
            )
        finally:
            await conn.close()
        return KeySummary(
            email=email,
            key_last4=last4(key),
            key_version=KEY_VERSION,
            updated_at=updated_at,
        )

    async def revoke(self, user_sub: str, provider: str = PROVIDER_GOOGLE) -> bool:
        conn = await self._connect()
        try:
            status = await conn.execute(_DELETE, provider, user_sub)
        finally:
            await conn.close()
        # asyncpg returns the command tag, e.g. "DELETE 1".
        return status.rsplit(" ", 1)[-1].strip() not in ("", "0")


def build_key_store(
    dsn: str | None = None, master_raw: str | None = None
) -> KeyStore:
    """The store this deployment should use, or a NullKeyStore.

    Never raises for a missing configuration -- an unconfigured deployment is
    the normal state of every deployment until ops sets the three variables,
    and it must boot and serve keyed callers exactly as it does today. A
    *malformed* MCP_KEY_MASTER is different: that is a typo somebody needs to
    see, so it is logged loudly and then treated as unconfigured rather than
    silently encrypting under a key nobody meant.
    """
    dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
    raw_master = (
        master_raw if master_raw is not None else os.environ.get("MCP_KEY_MASTER", "")
    ).strip()
    if not dsn or not raw_master:
        return NullKeyStore()
    try:
        master = load_master_key(raw_master)
    except MasterKeyError as exc:
        logger.error(
            "MCP_KEY_MASTER is set but unusable (%s); the key store stays "
            "disabled and /connect will not be registered.",
            exc,
        )
        return NullKeyStore()
    return PostgresKeyStore(dsn, master)
