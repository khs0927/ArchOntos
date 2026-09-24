BEGIN;

-- Source identity is stable across revisions; byte hashes belong to artifacts/source versions.
ALTER TABLE source_document DROP COLUMN IF EXISTS content_hash;

-- Explicitly connect executable rule versions to the assertions that justify them.
CREATE TABLE IF NOT EXISTS rule_assertion (
  rule_version_id uuid NOT NULL REFERENCES rule_version(id) ON DELETE CASCADE,
  assertion_id uuid NOT NULL REFERENCES assertion(id),
  role text NOT NULL DEFAULT 'basis',
  ordinal integer NOT NULL DEFAULT 0,
  PRIMARY KEY (rule_version_id, assertion_id, role)
);

CREATE INDEX IF NOT EXISTS ix_rule_assertion_assertion
  ON rule_assertion(assertion_id, role);

COMMIT;
