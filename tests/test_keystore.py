"""
The stored-key layer: encryption, the round trip, and every way it must fail
closed.

The thing being protected here is somebody else's paid RapidAPI subscription.
A store that returns the wrong key, or a plaintext key, or a key after
Disconnect, is not a bug in a page -- it is a stranger's search billed to a
customer. So most of this file is about the failure directions.
"""

import base64
import os

import pytest

from src.keystore import (
    KEY_VERSION,
    KeyDecryptionError,
    KeyStoreUnavailable,
    MasterKeyError,
    MemoryKeyStore,
    NullKeyStore,
    build_key_store,
    decrypt_key,
    encrypt_key,
    last4,
    load_master_key,
)

KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ1234"
OTHER_KEY = "9876543210zyxwvutsrqponmlkjihgfedcbaZYXWVU987654"


def a_master() -> bytes:
    return os.urandom(32)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class TestMasterKey:
    def test_standard_base64_of_32_bytes(self):
        raw = a_master()
        assert load_master_key(b64(raw)) == raw

    def test_urlsafe_base64_is_accepted(self):
        """The same key pasted out of a different generator.

        `openssl rand -base64 32` and `python -c secrets.token_urlsafe` give
        different alphabets for the same 32 bytes, and a deployment that
        rejected one of them would fail with "not 32 bytes" for a key that
        is exactly 32 bytes.
        """
        raw = bytes(range(32))
        assert load_master_key(base64.urlsafe_b64encode(raw).decode()) == raw

    def test_unpadded_is_accepted(self):
        raw = a_master()
        assert load_master_key(b64(raw).rstrip("=")) == raw

    def test_quoted_value_is_accepted(self):
        """Both .env files in this repo quote their values (settings._strip_quotes)."""
        raw = a_master()
        assert load_master_key(f'"{b64(raw)}"') == raw

    def test_missing_raises(self):
        with pytest.raises(MasterKeyError):
            load_master_key("")

    def test_wrong_length_raises(self):
        with pytest.raises(MasterKeyError):
            load_master_key(b64(os.urandom(16)))

    def test_not_base64_raises(self):
        with pytest.raises(MasterKeyError):
            load_master_key("this is not base64 at all !!!")


class TestEncryption:
    def test_round_trip(self):
        master = a_master()
        ciphertext, nonce = encrypt_key(KEY, master)
        assert decrypt_key(ciphertext, nonce, master) == KEY

    def test_ciphertext_does_not_contain_the_key(self):
        master = a_master()
        ciphertext, _ = encrypt_key(KEY, master)
        assert KEY.encode() not in ciphertext

    def test_two_writes_of_the_same_key_differ(self):
        """A fresh nonce per write. Equal ciphertexts would tell anyone with
        a table dump which users share a key."""
        master = a_master()
        first, _ = encrypt_key(KEY, master)
        second, _ = encrypt_key(KEY, master)
        assert first != second

    def test_wrong_master_key_fails_closed(self):
        """The rotation case, and the stolen-dump case. AES-GCM
        authenticates, so this raises rather than returning garbage that
        would be forwarded to RapidAPI as somebody's key."""
        ciphertext, nonce = encrypt_key(KEY, a_master())
        with pytest.raises(KeyDecryptionError):
            decrypt_key(ciphertext, nonce, a_master())

    def test_tampered_ciphertext_fails_closed(self):
        master = a_master()
        ciphertext, nonce = encrypt_key(KEY, master)
        flipped = bytes([ciphertext[0] ^ 0x01]) + ciphertext[1:]
        with pytest.raises(KeyDecryptionError):
            decrypt_key(flipped, nonce, master)

    def test_wrong_nonce_fails_closed(self):
        master = a_master()
        ciphertext, _ = encrypt_key(KEY, master)
        with pytest.raises(KeyDecryptionError):
            decrypt_key(ciphertext, os.urandom(12), master)

    def test_refuses_to_encrypt_nothing(self):
        with pytest.raises(ValueError):
            encrypt_key("", a_master())

    def test_last4_is_the_only_readable_fragment(self):
        assert last4(KEY) == KEY[-4:]
        assert len(last4(KEY)) == 4
        assert last4("ab") == ""


class TestMemoryStore:
    @pytest.mark.asyncio
    async def test_put_then_get_returns_the_key(self):
        store = MemoryKeyStore(a_master())
        summary = await store.put("sub-1", "a@example.test", KEY)
        assert summary.key_last4 == KEY[-4:]
        assert summary.key_version == KEY_VERSION

        stored = await store.get("sub-1")
        assert stored is not None
        assert stored.key == KEY
        assert stored.email == "a@example.test"
        assert stored.key_last4 == KEY[-4:]

    @pytest.mark.asyncio
    async def test_summary_never_carries_a_key(self):
        store = MemoryKeyStore(a_master())
        await store.put("sub-1", "a@example.test", KEY)
        summary = await store.summary("sub-1")
        assert summary is not None
        assert KEY not in repr(summary)

    @pytest.mark.asyncio
    async def test_unknown_user_is_none_not_an_error(self):
        store = MemoryKeyStore(a_master())
        assert await store.get("nobody") is None
        assert await store.summary("nobody") is None

    @pytest.mark.asyncio
    async def test_put_replaces(self):
        store = MemoryKeyStore(a_master())
        await store.put("sub-1", "a@example.test", KEY)
        await store.put("sub-1", "a@example.test", OTHER_KEY)
        stored = await store.get("sub-1")
        assert stored is not None and stored.key == OTHER_KEY

    @pytest.mark.asyncio
    async def test_revoke_removes_the_row(self):
        store = MemoryKeyStore(a_master())
        await store.put("sub-1", "a@example.test", KEY)
        assert await store.revoke("sub-1") is True
        assert await store.get("sub-1") is None
        # Idempotent: pressing Disconnect twice is not an error.
        assert await store.revoke("sub-1") is False

    @pytest.mark.asyncio
    async def test_two_users_do_not_see_each_other(self):
        store = MemoryKeyStore(a_master())
        await store.put("sub-1", "a@example.test", KEY)
        await store.put("sub-2", "b@example.test", OTHER_KEY)
        first = await store.get("sub-1")
        second = await store.get("sub-2")
        assert first is not None and second is not None
        assert first.key == KEY and second.key == OTHER_KEY

    @pytest.mark.asyncio
    async def test_provider_is_part_of_the_identity(self):
        """`sub` is only unique within an issuer. Two providers handing out
        the same sub must not resolve to one another's key."""
        store = MemoryKeyStore(a_master())
        await store.put("shared-sub", "a@example.test", KEY, provider="google")
        assert await store.get("shared-sub", provider="other") is None


class TestNullStore:
    @pytest.mark.asyncio
    async def test_reads_are_empty_not_errors(self):
        """An unconfigured deployment must serve keyed callers unchanged, so
        a read is simply "this caller has no stored key"."""
        store = NullKeyStore()
        assert store.available is False
        assert await store.get("sub-1") is None
        assert await store.summary("sub-1") is None
        assert await store.revoke("sub-1") is False

    @pytest.mark.asyncio
    async def test_writes_raise_rather_than_silently_dropping(self):
        with pytest.raises(KeyStoreUnavailable):
            await NullKeyStore().put("sub-1", "a@example.test", KEY)


class TestBuildKeyStore:
    def test_nothing_configured_is_a_null_store(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("MCP_KEY_MASTER", raising=False)
        assert build_key_store().available is False

    def test_database_without_a_master_key_is_a_null_store(self):
        assert build_key_store("postgres://x/y", "").available is False

    def test_master_key_without_a_database_is_a_null_store(self):
        assert build_key_store("", b64(a_master())).available is False

    def test_a_malformed_master_key_disables_rather_than_boots_broken(self, caplog):
        """A typo in MCP_KEY_MASTER must not take the deployment down, and
        must not encrypt under something nobody meant. It logs loudly and the
        feature stays off."""
        store = build_key_store("postgres://x/y", "not-base64-!!!")
        assert store.available is False

    def test_both_configured_builds_a_postgres_store(self):
        store = build_key_store("postgres://user:pw@host/db", b64(a_master()))
        assert store.available is True
        assert type(store).__name__ == "PostgresKeyStore"
