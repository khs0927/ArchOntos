BEGIN;

-- MVP-1: DXF/CAD object normalization (docs/MVP1-CAD.md).
-- cad_artifact     one uploaded drawing byte revision (project + source key + sha256), linked to
--                  the immutable content-addressed `artifact` row; jurisdiction-scoped (RLS).
-- cad_revision     one parse of that artifact under a parser_revision (parser + mapping rules).
-- cad_entity       raw modelspace entities of a revision (handle, layer, type, block, geometry).
-- arch_object      normalized architectural objects (wall/door/window/space/...) with numeric
--                  properties carrying explicit units, plus fact_units for the rule engine.
-- arch_object_source provenance: which raw entities an object was derived from.
-- Every table is append-only: ids are deterministic (uuid5 of bytes + parser revision), so a
-- re-upload never rewrites; UPDATE/DELETE are rejected by trigger for every role and the
-- service role additionally holds no UPDATE/DELETE privilege. Dependent rows are reached
-- through cad_artifact joins, which carry the jurisdiction policy (same as source_document).

CREATE TABLE cad_artifact (
  id uuid PRIMARY KEY,
  artifact_id uuid NOT NULL REFERENCES artifact(id),
  project_id text NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
  source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 300),
  source_id char(64) NOT NULL CHECK (source_id ~ '^[0-9a-f]{64}$'),
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  file_name text,
  byte_size bigint NOT NULL CHECK (byte_size > 0),
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, sha256)
);
CREATE INDEX ix_cad_artifact_project ON cad_artifact(project_id, source_key);

CREATE TABLE cad_revision (
  id uuid PRIMARY KEY,
  cad_artifact_id uuid NOT NULL REFERENCES cad_artifact(id),
  revision_label text NOT NULL CHECK (length(revision_label) BETWEEN 1 AND 100),
  parser_revision text NOT NULL,
  parser_revision_id char(64) NOT NULL CHECK (parser_revision_id ~ '^[0-9a-f]{64}$'),
  rule_set_json jsonb NOT NULL,
  units text NOT NULL,
  units_assumed boolean NOT NULL,
  dxf_version text NOT NULL,
  entity_count integer NOT NULL CHECK (entity_count >= 0),
  object_count integer NOT NULL CHECK (object_count >= 0),
  unmapped_entity_count integer NOT NULL CHECK (unmapped_entity_count >= 0),
  content_digest char(64) NOT NULL,
  created_by text NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cad_artifact_id, parser_revision)
);

CREATE TABLE cad_entity (
  id uuid PRIMARY KEY,
  revision_id uuid NOT NULL REFERENCES cad_revision(id),
  handle text NOT NULL,
  layer text NOT NULL,
  entity_type text NOT NULL,
  block_name text,
  geometry_json jsonb NOT NULL,
  bbox_min_x double precision,
  bbox_min_y double precision,
  bbox_max_x double precision,
  bbox_max_y double precision,
  entity_hash char(64) NOT NULL,
  UNIQUE (revision_id, handle)
);
CREATE INDEX ix_cad_entity_layer ON cad_entity(revision_id, layer, id);

CREATE TABLE arch_object (
  id uuid PRIMARY KEY,
  revision_id uuid NOT NULL REFERENCES cad_revision(id),
  kind text NOT NULL
    CHECK (kind IN ('wall','door','window','space','stair','column','opening')),
  rule_id text NOT NULL,
  layer text NOT NULL,
  properties_json jsonb NOT NULL,
  fact_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  geometry_json jsonb NOT NULL,
  source_handles text[] NOT NULL,
  subject_ref_json jsonb NOT NULL,
  object_hash char(64) NOT NULL
);
CREATE INDEX ix_arch_object_kind ON arch_object(revision_id, kind, id);

CREATE TABLE arch_object_source (
  arch_object_id uuid NOT NULL REFERENCES arch_object(id),
  cad_entity_id uuid NOT NULL REFERENCES cad_entity(id),
  role text NOT NULL DEFAULT 'geometry' CHECK (role IN ('geometry','label')),
  PRIMARY KEY (arch_object_id, cad_entity_id)
);
CREATE INDEX ix_arch_object_source_entity ON arch_object_source(cad_entity_id);

CREATE FUNCTION cad_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (immutable CAD revisions)', TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END
$$;

DO $$
DECLARE
  t text;
  s text := current_schema();
BEGIN
  FOREACH t IN ARRAY ARRAY['cad_artifact','cad_revision','cad_entity','arch_object',
                           'arch_object_source'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION cad_reject_mutation()', t || '_append_only', t);
    EXECUTE format('GRANT SELECT, INSERT ON %I.%I TO archontos_app', s, t);
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I.%I FROM archontos_app', s, t);
  END LOOP;
END
$$;

ALTER TABLE cad_artifact ENABLE ROW LEVEL SECURITY;

CREATE POLICY cad_artifact_jurisdiction ON cad_artifact
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
