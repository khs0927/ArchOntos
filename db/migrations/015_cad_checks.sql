BEGIN;

-- MVP-2: regulation-to-CAD checks (docs/MVP2-CAD-CHECKS.md).
-- cad_check_run     one evaluation of the active, approved rule versions of a jurisdiction
--                   against one cad_revision on a reference date (plus optional land-parcel
--                   facts). Deterministic id: the same inputs never produce a second run.
-- cad_check_result  one PASS/FAIL/REVIEW verdict per applicable rule_version, with the rule's
--                   jurisdiction and effective interval, the evidence locator(s) of its basis
--                   assertions, and the offending arch_object ids and CAD entity handles.
-- The existing evaluation/decision tables key on object_version and cannot carry CAD object
-- ids, entity handles or a run, so CAD checks get their own append-only tables. Reads join
-- through cad_check_run, which carries the same fail-closed jurisdiction policy as
-- cad_artifact (migration 014).

CREATE TABLE cad_check_run (
  id uuid PRIMARY KEY,
  revision_id uuid NOT NULL REFERENCES cad_revision(id),
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  reference_date date NOT NULL,
  land_parcel_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  facts_json jsonb NOT NULL,
  fact_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  inputs_digest char(64) NOT NULL CHECK (inputs_digest ~ '^[0-9a-f]{64}$'),
  checker_version text NOT NULL,
  rule_count integer NOT NULL CHECK (rule_count >= 0),
  pass_count integer NOT NULL CHECK (pass_count >= 0),
  fail_count integer NOT NULL CHECK (fail_count >= 0),
  review_count integer NOT NULL CHECK (review_count >= 0),
  not_applicable_count integer NOT NULL CHECK (not_applicable_count >= 0),
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cad_check_run_revision ON cad_check_run(revision_id, id);

CREATE TABLE cad_check_result (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES cad_check_run(id),
  rule_version_id uuid NOT NULL REFERENCES rule_version(id),
  rule_title text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('PASS','FAIL','REVIEW')),
  reason text,
  details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  jurisdiction_code text NOT NULL,
  valid_from date NOT NULL,
  valid_to date,
  binding boolean NOT NULL,
  evidence_locator_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  fact_paths text[] NOT NULL DEFAULT '{}',
  arch_object_ids uuid[] NOT NULL DEFAULT '{}',
  entity_handles text[] NOT NULL DEFAULT '{}',
  UNIQUE (run_id, rule_version_id)
);
CREATE INDEX ix_cad_check_result_run ON cad_check_result(run_id, outcome, id);
CREATE INDEX ix_cad_check_result_objects ON cad_check_result USING gin(arch_object_ids);

DO $$
DECLARE
  t text;
  s text := current_schema();
BEGIN
  FOREACH t IN ARRAY ARRAY['cad_check_run','cad_check_result'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION cad_reject_mutation()', t || '_append_only', t);
    EXECUTE format('GRANT SELECT, INSERT ON %I.%I TO archontos_app', s, t);
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.%I FROM archontos_app', s, t);
  END LOOP;
END
$$;

ALTER TABLE cad_check_run ENABLE ROW LEVEL SECURITY;

CREATE POLICY cad_check_run_jurisdiction ON cad_check_run
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
