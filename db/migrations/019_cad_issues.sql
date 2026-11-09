BEGIN;

-- MVP-5: CAD issues, BCF export, reports and marker actions (docs/MVP5-BCF-REPORTS-ACTIONS.md).
-- cad_issue          one issue per (check run, rule_version, offending arch_object) derived from
--                    FAIL/REVIEW cad_check_results (origin 'check') or MVP-3 re-run regressions
--                    (origin 'regression', diff_id). Deterministic id; never edited.
-- cad_issue_status   append-only status observations (open | resolved | stale) derived from
--                    superseding lineage diffs; the latest row (derived_at, id) is current.
-- cad_issue_note     append-only free-text notes on an issue.
-- cad_issue_artifact generated outputs (bcf | report | markers) linked to a run and issues.
-- All tables are append-only (cad_reject_mutation from migration 014). cad_issue and
-- cad_issue_artifact carry the fail-closed jurisdiction policy; status/notes are read through
-- cad_issue joins.

CREATE TABLE cad_issue (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES cad_check_run(id),
  revision_id uuid NOT NULL REFERENCES cad_revision(id),
  rule_version_id uuid NOT NULL REFERENCES rule_version(id),
  arch_object_id uuid REFERENCES arch_object(id),
  object_kind text,
  label text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('check','regression')),
  diff_id uuid REFERENCES cad_revision_diff(id),
  outcome text NOT NULL CHECK (outcome IN ('FAIL','REVIEW')),
  rule_title text NOT NULL,
  reason text,
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  reference_date date NOT NULL,
  valid_from date,
  valid_to date,
  binding boolean NOT NULL,
  evidence_locator_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  fact_paths text[] NOT NULL DEFAULT '{}',
  entity_handles text[] NOT NULL DEFAULT '{}',
  source_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  bbox_json jsonb,
  deriver_version text NOT NULL,
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((origin = 'regression') = (diff_id IS NOT NULL))
);
CREATE INDEX ix_cad_issue_run ON cad_issue(run_id, id);
CREATE INDEX ix_cad_issue_revision ON cad_issue(revision_id, id);
CREATE INDEX ix_cad_issue_rule ON cad_issue(rule_version_id, id);

CREATE TABLE cad_issue_status (
  id uuid PRIMARY KEY,
  issue_id uuid NOT NULL REFERENCES cad_issue(id),
  status text NOT NULL CHECK (status IN ('open','resolved','stale')),
  basis_diff_id uuid REFERENCES cad_revision_diff(id),
  derived_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  derived_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((status = 'open') = (basis_diff_id IS NULL))
);
CREATE INDEX ix_cad_issue_status_issue ON cad_issue_status(issue_id, derived_at, id);

CREATE TABLE cad_issue_note (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_id uuid NOT NULL REFERENCES cad_issue(id),
  body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cad_issue_note_issue ON cad_issue_note(issue_id, created_at);

CREATE TABLE cad_issue_artifact (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('bcf','report','markers')),
  artifact_id uuid NOT NULL REFERENCES artifact(id),
  run_id uuid REFERENCES cad_check_run(id),
  revision_id uuid REFERENCES cad_revision(id),
  action_id uuid,
  issue_ids uuid[] NOT NULL DEFAULT '{}',
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  media_type text NOT NULL,
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cad_issue_artifact_run ON cad_issue_artifact(run_id, kind, id);

DO $$
DECLARE
  t text;
  s text := current_schema();
BEGIN
  FOREACH t IN ARRAY ARRAY['cad_issue','cad_issue_status','cad_issue_note','cad_issue_artifact'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION cad_reject_mutation()', t || '_append_only', t);
    EXECUTE format('GRANT SELECT, INSERT ON %I.%I TO archontos_app', s, t);
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.%I FROM archontos_app', s, t);
  END LOOP;
END
$$;

ALTER TABLE cad_issue ENABLE ROW LEVEL SECURITY;
ALTER TABLE cad_issue_artifact ENABLE ROW LEVEL SECURITY;

CREATE POLICY cad_issue_jurisdiction ON cad_issue
  FOR ALL
  USING (
    jurisdiction_code = ANY (
      string_to_array(NULLIF(current_setting('app.allowed_jurisdictions', true), ''), ',')
    )
  )
  WITH CHECK (
    jurisdiction_code = ANY (
      string_to_array(NULLIF(current_setting('app.allowed_jurisdictions', true), ''), ',')
    )
  );

CREATE POLICY cad_issue_artifact_jurisdiction ON cad_issue_artifact
  FOR ALL
  USING (
    jurisdiction_code = ANY (
      string_to_array(NULLIF(current_setting('app.allowed_jurisdictions', true), ''), ',')
    )
  )
  WITH CHECK (
    jurisdiction_code = ANY (
      string_to_array(NULLIF(current_setting('app.allowed_jurisdictions', true), ''), ',')
    )
  );

COMMIT;
