BEGIN;

ALTER TABLE assertion
  ADD COLUMN IF NOT EXISTS assertion_key text;

ALTER TABLE assertion
  ADD COLUMN IF NOT EXISTS reviewer_id text;

ALTER TABLE assertion
  ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

ALTER TABLE assertion
  ADD COLUMN IF NOT EXISTS review_note text;

-- Remove the legacy state check before translating reviewed -> approved.
ALTER TABLE assertion
  DROP CONSTRAINT IF EXISTS assertion_review_status_check;

UPDATE assertion
SET review_status = 'approved'
WHERE review_status = 'reviewed';

ALTER TABLE assertion
  ADD CONSTRAINT assertion_review_status_check
  CHECK (review_status IN ('unreviewed','approved','rejected','contested'));

CREATE UNIQUE INDEX IF NOT EXISTS ux_assertion_evidence_key
  ON assertion(evidence_span_id, assertion_key)
  WHERE assertion_key IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'uq_evidence_span_id_source_version'
  ) THEN
    ALTER TABLE evidence_span
      ADD CONSTRAINT uq_evidence_span_id_source_version
      UNIQUE (id, source_version_id);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'assertion_evidence_source_fk'
  ) THEN
    ALTER TABLE assertion
      ADD CONSTRAINT assertion_evidence_source_fk
      FOREIGN KEY (evidence_span_id, source_version_id)
      REFERENCES evidence_span(id, source_version_id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS assertion_review (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assertion_id uuid NOT NULL REFERENCES assertion(id) ON DELETE CASCADE,
  previous_status text NOT NULL
    CHECK (previous_status IN ('unreviewed','approved','rejected','contested')),
  new_status text NOT NULL
    CHECK (new_status IN ('approved','rejected','contested')),
  reviewer_id text NOT NULL,
  note text,
  reviewed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_assertion_review_history
  ON assertion_review(assertion_id, reviewed_at DESC);

COMMIT;
