BEGIN;

-- Issue notes API + BCF import (docs/MVP5-BCF-REPORTS-ACTIONS.md).
-- cad_issue_note gains provenance columns so imported BCF comments / topic statuses can be
-- stored append-only and idempotently:
--   kind           note (typed by an actor) | bcf_comment | bcf_status
--   external_guid  BCF comment guid (bcf_comment) or topic guid + status (bcf_status digest)
--   external_author / external_at  author and date written in the BCF file
-- (issue_id, external_guid) is unique when set, so re-importing the same file adds nothing.
-- Existing rows keep kind 'note' through the column default (no UPDATE; table is append-only).

ALTER TABLE cad_issue_note
  ADD COLUMN kind text NOT NULL DEFAULT 'note'
    CHECK (kind IN ('note','bcf_comment','bcf_status')),
  ADD COLUMN external_guid text CHECK (length(external_guid) BETWEEN 1 AND 200),
  ADD COLUMN external_author text CHECK (length(external_author) <= 200),
  ADD COLUMN external_at timestamptz,
  ADD CHECK ((kind = 'note') = (external_guid IS NULL));

CREATE UNIQUE INDEX ux_cad_issue_note_external
  ON cad_issue_note(issue_id, external_guid) WHERE external_guid IS NOT NULL;
CREATE INDEX ix_cad_issue_note_page ON cad_issue_note(issue_id, created_at, id);

COMMIT;
