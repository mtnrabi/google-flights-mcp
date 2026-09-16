-- The keyless allowance on the paid MCP server: how many backend searches one
-- signed-in Google account has spent on our key, on one UTC day.
--
-- Idempotent, and safe to run BEFORE the code that uses it ships: nothing
-- reads or writes this table until PAID_TRIAL_DAY_CAP and a trial key are set
-- on the deployment.
--
-- One row per account per day. Never a single global counter -- see
-- src/trialstore.py for why that shape is the bug, not the simplification.
--
-- The primary key is load-bearing beyond uniqueness: the allowance is taken by
-- a single INSERT ... ON CONFLICT (provider, user_sub, day) DO UPDATE ... WHERE
-- searches + n <= cap RETURNING searches, so two concurrent reservations
-- serialise on this row and at most one of them can win. Changing the key
-- changes that guarantee.
-- `day` is a UTC date, written by the application (src/trial.utc_day), not by
-- the database, so the rollover cannot depend on the session's timezone.

CREATE TABLE IF NOT EXISTS paid_trial_usage (
    provider    text        NOT NULL DEFAULT 'google',
    user_sub    text        NOT NULL,
    day         date        NOT NULL,
    searches    integer     NOT NULL DEFAULT 0,
    -- Kept for support ("which account is this?") and for nothing else. Never
    -- required: a row is valid with a NULL email, and the counter is keyed on
    -- the provider subject, which is stable across an address change.
    email       text,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, user_sub, day)
);

-- "How much did the allowance cost yesterday" and "who is using it" are the
-- two questions this table gets asked, and both scan by day.
CREATE INDEX IF NOT EXISTS paid_trial_usage_day_idx ON paid_trial_usage (day);

-- Rows older than a couple of months answer no question anyone asks; the
-- allowance resets daily and there is no billing to reconcile. No scheduled
-- job is shipped with this migration -- at this volume it can wait -- but the
-- statement is written down so it does not have to be reinvented:
--
--   DELETE FROM paid_trial_usage WHERE day < (now() AT TIME ZONE 'utc')::date - 62;
