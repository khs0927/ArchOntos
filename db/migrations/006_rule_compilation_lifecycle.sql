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

-- Existing rule versions inherit the legal source interval.
UPDATE rule_version rv
SET valid_to = sv.effective_to
FROM rule r
JOIN source_version sv ON sv.id = r.source_version_id
WHERE r.id = rv.rule_id
  AND rv.valid_to IS DISTINCT FROM sv.effective_to;

-- Fail closed on legacy executable rules without fully approved basis assertions.
UPDATE rule_version rv
SET status = 'suspended'
WHERE NOT EXISTS (
    SELECT 1
    FROM rule_assertion ra
    WHERE ra.rule_version_id = rv.id
      AND ra.role = 'basis'
)
OR EXISTS (
    SELECT 1
    FROM rule_assertion ra
    JOIN assertion a ON a.id = ra.assertion_id
    WHERE ra.rule_version_id = rv.id
      AND ra.role = 'basis'
      AND a.review_status <> 'approved'
);

CREATE INDEX IF NOT EXISTS ix_rule_version_status
  ON rule_version(status, valid_from DESC);

COMMIT;
