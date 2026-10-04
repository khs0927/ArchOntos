BEGIN;

-- Stable evidence identity is scoped to one immutable source version.
-- Existing rows remain valid during rollout; new normalizers must populate evidence_key.
ALTER TABLE evidence_span
  ADD COLUMN IF NOT EXISTS evidence_key text;

ALTER TABLE evidence_span
  ADD COLUMN IF NOT EXISTS normalized_text_hash text;

CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_source_key
  ON evidence_span(source_version_id, evidence_key)
  WHERE evidence_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_evidence_text_hash
  ON evidence_span(normalized_text_hash)
  WHERE normalized_text_hash IS NOT NULL;

COMMIT;
