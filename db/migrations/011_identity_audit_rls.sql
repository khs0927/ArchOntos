BEGIN;

-- P3 identity groundwork.
-- 1. Who proposed an action. Existing rows predate identity and stay 'system'.
ALTER TABLE action ADD COLUMN created_by text NOT NULL DEFAULT 'system';

-- 2. audit_log is append-only for every non-superuser role, including the table owner
--    (FORCE). SELECT and INSERT are allowed; with no UPDATE/DELETE policy those are denied.
--    The actor defaults to the request identity the application sets per transaction with
--    set_config('app.actor', ..., true); see archontos.identity.
ALTER TABLE audit_log
  ALTER COLUMN actor SET DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system');

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_log_read ON audit_log FOR SELECT USING (true);
CREATE POLICY audit_log_append ON audit_log FOR INSERT WITH CHECK (true);

COMMIT;
