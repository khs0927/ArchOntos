BEGIN;

-- MVP-4: IFC / SketchUp model sources and cross-source identity (docs/MVP4-IFC-SKETCHUP.md).
-- cad_artifact / cad_revision  gain source_format (dxf | ifc | skp_json); revisions also keep
--                              model_meta_json (IFC schema, storeys, length unit). Existing rows
--                              read as 'dxf' / '{}' through the column defaults (no UPDATE).
-- arch_object / cad_object_change  accept the new 'slab' kind (IfcSlab).
-- arch_object_source_ref       source identity refs of an object (dxf_handle, ifc_guid,
--                              skp_persistent_id).
-- model_object_link            links two objects of different source formats that describe the
--                              same element (shared_ref: same ifc_guid, same project + revision
--                              label; or manual). Jurisdiction RLS like cad_artifact.
-- All new tables are append-only (cad_reject_mutation from migration 014).

ALTER TABLE cad_artifact
  ADD COLUMN source_format text NOT NULL DEFAULT 'dxf'
    CHECK (source_format IN ('dxf','ifc','skp_json'));

ALTER TABLE cad_revision
  ADD COLUMN source_format text NOT NULL DEFAULT 'dxf'
    CHECK (source_format IN ('dxf','ifc','skp_json')),
  ADD COLUMN model_meta_json jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE arch_object DROP CONSTRAINT arch_object_kind_check;
ALTER TABLE arch_object ADD CONSTRAINT arch_object_kind_check
  CHECK (kind IN ('wall','door','window','space','stair','column','opening','slab'));
ALTER TABLE cad_object_change DROP CONSTRAINT cad_object_change_kind_check;
ALTER TABLE cad_object_change ADD CONSTRAINT cad_object_change_kind_check
  CHECK (kind IN ('wall','door','window','space','stair','column','opening','slab'));

CREATE TABLE arch_object_source_ref (
  arch_object_id uuid NOT NULL REFERENCES arch_object(id),
  ref_kind text NOT NULL CHECK (ref_kind IN ('dxf_handle','ifc_guid','skp_persistent_id')),
  ref_value text NOT NULL CHECK (length(ref_value) BETWEEN 1 AND 128),
  PRIMARY KEY (arch_object_id, ref_kind, ref_value)
);
CREATE INDEX ix_arch_object_source_ref_value ON arch_object_source_ref(ref_kind, ref_value);

CREATE TABLE model_object_link (
  id uuid PRIMARY KEY,
  project_id text NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  object_a_id uuid NOT NULL REFERENCES arch_object(id),
  object_b_id uuid NOT NULL REFERENCES arch_object(id),
  revision_a_id uuid NOT NULL REFERENCES cad_revision(id),
  revision_b_id uuid NOT NULL REFERENCES cad_revision(id),
  method text NOT NULL CHECK (method IN ('shared_ref','manual')),
  ref_kind text,
  ref_value text,
  note text CHECK (note IS NULL OR length(note) <= 1000),
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (object_a_id < object_b_id),
  CHECK (revision_a_id <> revision_b_id),
  CHECK ((method = 'shared_ref') = (ref_kind IS NOT NULL AND ref_value IS NOT NULL)),
  UNIQUE (object_a_id, object_b_id, method)
);
CREATE INDEX ix_model_object_link_a ON model_object_link(object_a_id);
CREATE INDEX ix_model_object_link_b ON model_object_link(object_b_id);
CREATE INDEX ix_model_object_link_project ON model_object_link(project_id, id);

DO $$
DECLARE
  t text;
  s text := current_schema();
BEGIN
  FOREACH t IN ARRAY ARRAY['arch_object_source_ref','model_object_link'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION cad_reject_mutation()', t || '_append_only', t);
    EXECUTE format('GRANT SELECT, INSERT ON %I.%I TO archontos_app', s, t);
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.%I FROM archontos_app', s, t);
  END LOOP;
END
$$;

ALTER TABLE model_object_link ENABLE ROW LEVEL SECURITY;

CREATE POLICY model_object_link_jurisdiction ON model_object_link
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
