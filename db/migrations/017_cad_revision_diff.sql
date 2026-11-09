BEGIN;

-- MVP-3: permit-vs-construction / revision diff (docs/MVP3-REVISION-DIFF.md).
-- cad_revision_diff  one comparison of two cad_revisions (same lineage or an explicit pair)
--                    under a tolerance set. Deterministic id: the same revisions, tolerances,
--                    rerun flag and differ version never produce a second diff.
-- cad_object_change  one added/removed/modified/unchanged row per matched or unmatched
--                    arch_object, with per-property deltas (units in each delta) and the
--                    cad_check_results affected by the change (impact_json; base results
--                    are stale: bound to the old revision, never carried over).
-- Both tables are append-only (cad_reject_mutation from migration 014). The diff carries the
-- same fail-closed jurisdiction policy as cad_artifact; changes are read through it.

CREATE TABLE cad_revision_diff (
  id uuid PRIMARY KEY,
  base_revision_id uuid NOT NULL REFERENCES cad_revision(id),
  target_revision_id uuid NOT NULL REFERENCES cad_revision(id),
  relation text NOT NULL CHECK (relation IN ('lineage','paired')),
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  tolerances_json jsonb NOT NULL,
  tolerances_digest char(64) NOT NULL CHECK (tolerances_digest ~ '^[0-9a-f]{64}$'),
  rerun_checks boolean NOT NULL,
  differ_version text NOT NULL,
  base_object_count integer NOT NULL CHECK (base_object_count >= 0),
  target_object_count integer NOT NULL CHECK (target_object_count >= 0),
  added_count integer NOT NULL CHECK (added_count >= 0),
  removed_count integer NOT NULL CHECK (removed_count >= 0),
  modified_count integer NOT NULL CHECK (modified_count >= 0),
  unchanged_count integer NOT NULL CHECK (unchanged_count >= 0),
  summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  transitions_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  stale_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (base_revision_id <> target_revision_id)
);
CREATE INDEX ix_cad_revision_diff_base ON cad_revision_diff(base_revision_id, id);
CREATE INDEX ix_cad_revision_diff_target ON cad_revision_diff(target_revision_id, id);

CREATE TABLE cad_object_change (
  id uuid PRIMARY KEY,
  diff_id uuid NOT NULL REFERENCES cad_revision_diff(id),
  change_type text NOT NULL CHECK (change_type IN ('added','removed','modified','unchanged')),
  kind text NOT NULL
    CHECK (kind IN ('wall','door','window','space','stair','column','opening')),
  base_object_id uuid REFERENCES arch_object(id),
  target_object_id uuid REFERENCES arch_object(id),
  match_method text CHECK (match_method IN ('handle','content','geometry')),
  match_score double precision,
  label text NOT NULL,
  base_handles text[] NOT NULL DEFAULT '{}',
  target_handles text[] NOT NULL DEFAULT '{}',
  deltas_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  impact_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (
    (change_type = 'added' AND base_object_id IS NULL AND target_object_id IS NOT NULL)
    OR (change_type = 'removed' AND base_object_id IS NOT NULL AND target_object_id IS NULL)
    OR (change_type IN ('modified','unchanged')
        AND base_object_id IS NOT NULL AND target_object_id IS NOT NULL)
  )
);
CREATE INDEX ix_cad_object_change_diff ON cad_object_change(diff_id, change_type, kind, id);

DO $$
DECLARE
  t text;
  s text := current_schema();
BEGIN
  FOREACH t IN ARRAY ARRAY['cad_revision_diff','cad_object_change'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION cad_reject_mutation()', t || '_append_only', t);
    EXECUTE format('GRANT SELECT, INSERT ON %I.%I TO archontos_app', s, t);
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.%I FROM archontos_app', s, t);
  END LOOP;
END
$$;

ALTER TABLE cad_revision_diff ENABLE ROW LEVEL SECURITY;

CREATE POLICY cad_revision_diff_jurisdiction ON cad_revision_diff
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
