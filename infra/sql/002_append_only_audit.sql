-- Migration 002: make actions_log a true append-only ledger.
--
-- Dossier §21.4 requires that the audit table be append-only enforced by
-- the database, not by convention, because NFR-5 says every action must be
-- reconstructable and an application bug must not be able to violate that.
-- The previous design UPDATEd a row in place to record the outcome, which
-- meant the record of what was requested was overwritten by what happened.
--
-- New model: every state change is its own row.
--   decision-policy  inserts the decision      (dispatched / pending_approval / blocked)
--   executor         inserts the outcome       (executed / no_op / blocked_*), parent -> decision
--   rollback         inserts a reversal        (rolled_back), rollback_ref -> the executed row
-- Current state is derived by reading forward, never stored in place.

BEGIN;

-- rollback_ref previously held the prior-state JSON. Per the spec it is a
-- reference to the row being reversed, and the JSON belongs in prior_state.
ALTER TABLE actions_log RENAME COLUMN rollback_ref TO prior_state_legacy;

ALTER TABLE actions_log
    ADD COLUMN IF NOT EXISTS parent_action_id  UUID REFERENCES actions_log (id),
    ADD COLUMN IF NOT EXISTS rollback_ref      UUID REFERENCES actions_log (id),
    ADD COLUMN IF NOT EXISTS mode              TEXT NOT NULL DEFAULT 'autonomous'
        CHECK (mode IN ('autonomous', 'approved', 'dry_run')),
    ADD COLUMN IF NOT EXISTS target            JSONB,
    ADD COLUMN IF NOT EXISTS prior_state       JSONB,
    ADD COLUMN IF NOT EXISTS applied_state     JSONB,
    ADD COLUMN IF NOT EXISTS rollback_deadline TIMESTAMPTZ;

-- Carry any existing prior-state JSON across before dropping the old column.
UPDATE actions_log
   SET prior_state = prior_state_legacy::jsonb
 WHERE prior_state_legacy IS NOT NULL
   AND prior_state_legacy <> ''
   AND prior_state_legacy ~ '^\s*\{';

ALTER TABLE actions_log DROP COLUMN prior_state_legacy;

CREATE INDEX IF NOT EXISTS actions_log_executed_at_idx ON actions_log (executed_at DESC);
CREATE INDEX IF NOT EXISTS actions_log_parent_idx      ON actions_log (parent_action_id);

-- Enforced by the database, not by convention. Both the trigger and a
-- REVOKE are wanted in production because they fail differently: the grant
-- stops the application's normal path, the trigger stops anything arriving
-- with wider privileges, including a well-intentioned migration.
CREATE OR REPLACE FUNCTION deny_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'actions_log is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS actions_log_no_update ON actions_log;
CREATE TRIGGER actions_log_no_update BEFORE UPDATE ON actions_log
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

DROP TRIGGER IF EXISTS actions_log_no_delete ON actions_log;
CREATE TRIGGER actions_log_no_delete BEFORE DELETE ON actions_log
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

COMMIT;
