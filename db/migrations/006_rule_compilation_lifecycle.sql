BEGIN;

ALTER TABLE rule
  ADD COLUMN IF NOT EXISTS rule_key text;

CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_source_key
  ON rule(source_version_id, rule_key)
  WHERE rule_key IS NOT NULL;

ALTER TABLE rule_version
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';

ALTER TABLE rule_version
  DROP CONSTRAINT IF EXISTS rule_version_status_check;

ALTER TABLE rule_version
  ADD CONSTRAINT rule_version_status_check
  CHECK (status IN ('draft','active','suspended','retired'));

ALTER TABLE rule_version
  ADD COLUMN IF NOT EXISTS compiler_version text;

ALTER TABLE rule_version
  ADD COLUMN IF NOT EXISTS compiled_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_rule_version_status
  ON rule_version(status, valid_from DESC);

COMMIT;
