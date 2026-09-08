-- 003_mcp_oauth_hygiene.sql -- registration hygiene, refresh-token reuse
-- detection, and the email a signed-in caller is told they are signed in as.
--
--     psql "$DATABASE_URL" -f migrations/003_mcp_oauth_hygiene.sql
--
-- Idempotent, and safe to run BEFORE the code that reads it: every column
-- added here has a default, so the day-2 code keeps inserting and selecting
-- exactly as it does today against a table that already has them.
--
-- What each column is for
-- -----------------------
-- registered_ip / last_authorized_at
--     Dynamic client registration is open, as the MCP spec requires, so
--     anyone who can POST can make a row. Two caps now stand behind that:
--     a per-address and a per-day count (registered_ip), and a sweep that
--     deletes registrations which never became an authorization within 24
--     hours (last_authorized_at IS NULL). Four probe clients from the
--     2026-09-08 test ladder are exactly the shape that sweep removes.
--
-- family_id + a KEPT revoked_at on tokens
--     Refresh tokens already rotate. Rotation used to DELETE the old row,
--     which makes a replayed refresh token indistinguishable from one that
--     never existed. Now the row is kept and stamped, so presenting an
--     already-rotated refresh token is detectable -- and when it happens the
--     whole family descending from that one authorization is revoked, which
--     is the standard answer (OAuth 2.1 §4.14.2) to a stolen token.
--
-- user_email
--     A user who signed in through an MCP client but never pasted a key gets
--     told "you are signed in as <email>; paste your key once at ..." rather
--     than a wall of header instructions for a sign-in they already did.

ALTER TABLE mcp_oauth_clients
    ADD COLUMN IF NOT EXISTS registered_ip      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS last_authorized_at TIMESTAMPTZ;

-- The per-day registration caps count rows by age, and by age and address.
CREATE INDEX IF NOT EXISTS mcp_oauth_clients_created_idx
    ON mcp_oauth_clients (created_at);
CREATE INDEX IF NOT EXISTS mcp_oauth_clients_ip_idx
    ON mcp_oauth_clients (registered_ip, created_at);

ALTER TABLE mcp_oauth_codes
    ADD COLUMN IF NOT EXISTS user_email TEXT NOT NULL DEFAULT '';

ALTER TABLE mcp_oauth_tokens
    ADD COLUMN IF NOT EXISTS user_email TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS family_id  TEXT NOT NULL DEFAULT '';

-- Revoking a whole token family is one statement on this index.
CREATE INDEX IF NOT EXISTS mcp_oauth_tokens_family_idx
    ON mcp_oauth_tokens (family_id);

-- Housekeeping, now run by the server itself (a throttled sweep on
-- /oauth/register, at most once every 15 minutes per instance). These are the
-- same statements, for a Neon scheduled job or a hand run:
--
--     DELETE FROM mcp_oauth_codes  WHERE expires_at < now();
--     DELETE FROM mcp_oauth_tokens WHERE expires_at < now();
--     DELETE FROM mcp_oauth_clients c
--      WHERE c.created_at < now() - interval '24 hours'
--        AND c.last_authorized_at IS NULL
--        AND NOT EXISTS (SELECT 1 FROM mcp_oauth_tokens t
--                         WHERE t.client_id = c.client_id)
--        AND NOT EXISTS (SELECT 1 FROM mcp_oauth_codes k
--                         WHERE k.client_id = c.client_id);
