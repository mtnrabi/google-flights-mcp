-- Which RapidAPI account a connected key belongs to (2026-11-23).
--
-- The key alone does not say whose it is. The flights backend echoes the Hub's
-- `X-RapidAPI-User` back as `X-FP-User` on our own keyed calls (body tag
-- `_fp_source: paid-mcp`), and the server records it here, next to the Google
-- sign-in. That is the join that lets a RapidAPI leaver who used this server be
-- written to (Matan 2026-11-23: "a leaver who connected through one of our MCP
-- servers gave us their email").
--
-- Idempotent, and safe to run BEFORE or AFTER the code ships: the write is
-- best-effort and a missing column only logs a warning.

ALTER TABLE mcp_user_keys
    ADD COLUMN IF NOT EXISTS rapidapi_user         TEXT,
    ADD COLUMN IF NOT EXISTS rapidapi_user_seen_at TIMESTAMPTZ;
