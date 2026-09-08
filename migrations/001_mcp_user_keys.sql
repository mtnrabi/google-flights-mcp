-- 001_mcp_user_keys.sql -- the paid MCP server's connected-key store.
--
-- Run once against the Neon database DATABASE_URL points at (the same
-- database backend/src/data_access writes the attribution rollup to):
--
--     psql "$DATABASE_URL" -f migrations/001_mcp_user_keys.sql
--
-- Idempotent: safe to re-run, and safe to run before the code that reads it
-- is deployed.
--
-- What is in a row
-- ----------------
-- * `provider` + `user_sub` is the primary key. `sub` is only unique WITHIN
--   an issuer, so the issuer is part of the identity, not an assumption.
-- * `email` is stored so the user can see which Google account a key is
--   attached to, and so support can answer "which account did I connect
--   with". Nothing else from the Google profile is kept -- no name, no
--   picture, no locale.
-- * `key_ciphertext` + `key_nonce` are AES-256-GCM under MCP_KEY_MASTER.
--   The database never sees a plaintext RapidAPI key. Anyone with a dump and
--   no master key has nothing.
-- * `key_last4` is the only readable fragment, and it is what /connect shows.
-- * `key_version` names the master key a row was written under, so a key can
--   be rotated by writing new rows at N+1 while old rows still decrypt at N.
-- * `revoked_at` exists for an operational hold. "Disconnect" on /connect
--   DELETEs the row instead, because the page tells the user the key is
--   removed and a tombstone still holding the ciphertext would make that
--   sentence untrue.

CREATE TABLE IF NOT EXISTS mcp_user_keys (
    provider        TEXT        NOT NULL,
    user_sub        TEXT        NOT NULL,
    email           TEXT        NOT NULL DEFAULT '',
    key_ciphertext  BYTEA       NOT NULL,
    key_nonce       BYTEA       NOT NULL,
    key_last4       TEXT        NOT NULL DEFAULT '',
    key_version     INTEGER     NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    PRIMARY KEY (provider, user_sub)
);

-- The only query that is not by primary key: "who has connected", for the
-- weekly read. Partial, because a revoked row is not a connected user.
CREATE INDEX IF NOT EXISTS mcp_user_keys_live_idx
    ON mcp_user_keys (updated_at DESC)
 WHERE revoked_at IS NULL;
