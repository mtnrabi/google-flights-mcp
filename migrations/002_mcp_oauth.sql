-- 002_mcp_oauth.sql -- the paid MCP server's MCP-protocol OAuth state.
--
-- Run once against the Neon database DATABASE_URL points at, the same one
-- 001_mcp_user_keys.sql created `mcp_user_keys` in:
--
--     psql "$DATABASE_URL" -f migrations/002_mcp_oauth.sql
--
-- Idempotent, and safe to run before the code that reads it is deployed.
--
-- Why these three tables exist
-- ----------------------------
-- Day 1's /connect needed no server-side state: its session cookie and its
-- `fpk_` connect token are self-contained signed payloads, so any Vercel
-- instance can verify one without having seen it issued. MCP-protocol OAuth
-- cannot work that way:
--
--   * a dynamically-registered client registers on one instance and calls
--     /authorize on another, so `client_id` has to be written down;
--   * an authorization code must be single-use, which is a claim about a row
--     being consumed -- `DELETE ... RETURNING` is what makes it true;
--   * access and refresh tokens must be revocable, which a signed token is
--     not.
--
-- Nothing here is stored in the clear
-- -----------------------------------
-- Codes, access tokens, refresh tokens and client secrets are all SHA-256
-- hashes of values that were 32 bytes from os.urandom. A dump of this
-- database contains nothing replayable. No RapidAPI key is stored here at
-- all -- that lives in `mcp_user_keys`, encrypted, and is reached through
-- the `user_sub` these rows carry.

-- ── clients ─────────────────────────────────────────────────────────────
-- One row per dynamic client registration (RFC 7591). Registration is open,
-- as the MCP spec requires; a row is cheap and holds no secret, and an
-- unused client_id authorises nothing on its own -- every flow through it
-- still needs a signed-in Google account to approve it.
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id                   TEXT        PRIMARY KEY,
    client_name                 TEXT        NOT NULL DEFAULT '',
    redirect_uris               TEXT[]      NOT NULL,
    token_endpoint_auth_method  TEXT        NOT NULL DEFAULT 'none',
    scope                       TEXT        NOT NULL DEFAULT '',
    -- '' for a public client. Almost every MCP client is public: it runs on
    -- the user's machine and has nowhere to keep a secret. PKCE is what
    -- protects those, and this server requires PKCE from everybody.
    client_secret_hash          TEXT        NOT NULL DEFAULT '',
    metadata                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── authorization codes ─────────────────────────────────────────────────
-- Short-lived (10 minutes) and single-use. The PKCE *challenge* is stored;
-- the verifier never is, because storing it would defeat the point.
CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
    code_hash       TEXT        PRIMARY KEY,
    client_id       TEXT        NOT NULL,
    redirect_uri    TEXT        NOT NULL,
    code_challenge  TEXT        NOT NULL,
    scope           TEXT        NOT NULL DEFAULT '',
    -- The identity that approved it: the same (provider, user_sub) pair
    -- mcp_user_keys is keyed by, which is how a tool call authenticated with
    -- one of these tokens finds the user's stored RapidAPI key.
    user_sub        TEXT        NOT NULL,
    provider        TEXT        NOT NULL DEFAULT 'google',
    -- RFC 8707 resource indicator, as MCP 2025-06-18 requires clients to
    -- send. Kept so a token can never be replayed against a different
    -- resource than the one it was approved for.
    resource        TEXT        NOT NULL DEFAULT '',
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS mcp_oauth_codes_expiry_idx
    ON mcp_oauth_codes (expires_at);

-- ── tokens ──────────────────────────────────────────────────────────────
-- Access (1 hour) and refresh (30 days) in one table: they differ only in
-- `kind` and lifetime, and a lookup always states which kind it wants, so a
-- refresh token can never be presented as an access token.
CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
    token_hash   TEXT        PRIMARY KEY,
    kind         TEXT        NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id    TEXT        NOT NULL,
    user_sub     TEXT        NOT NULL,
    provider     TEXT        NOT NULL DEFAULT 'google',
    scope        TEXT        NOT NULL DEFAULT '',
    resource     TEXT        NOT NULL DEFAULT '',
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "log this account out of everything", which is what Disconnect on /connect
-- should mean and what a support request will ask for.
CREATE INDEX IF NOT EXISTS mcp_oauth_tokens_user_idx
    ON mcp_oauth_tokens (provider, user_sub);

CREATE INDEX IF NOT EXISTS mcp_oauth_tokens_expiry_idx
    ON mcp_oauth_tokens (expires_at);

-- Housekeeping. Expired rows are already ignored on read, so this is about
-- table size, not correctness. At this server's volume it can wait a long
-- time; run it by hand, or from a Neon scheduled job:
--
--     DELETE FROM mcp_oauth_codes  WHERE expires_at < now();
--     DELETE FROM mcp_oauth_tokens WHERE expires_at < now();
--
-- `OAuthStore.purge_expired()` runs exactly those two statements.
