BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE jurisdiction (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  parent_code text,
  level text NOT NULL CHECK (level IN ('national','metro','province','municipal','district'))
);

INSERT INTO jurisdiction(code, name, level)
VALUES ('KR', '대한민국', 'national')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE source_document (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_key text NOT NULL UNIQUE,
  title text NOT NULL,
  issuer text NOT NULL,
  jurisdiction_code text NOT NULL REFERENCES jurisdiction(code),
  document_type text NOT NULL CHECK (document_type IN ('statute','regulation','rule','ordinance','standard','guide')),
  source_url text,
  content_hash text NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artifact (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_type text NOT NULL,
  mime text NOT NULL,
  storage_uri text NOT NULL,
  content_hash text NOT NULL,
  byte_size bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(storage_uri, content_hash)
);

CREATE TABLE source_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES source_document(id),
  artifact_id uuid REFERENCES artifact(id),
  version_label text NOT NULL,
  effective_from date NOT NULL,
  effective_to date,
  promulgated_at date,
  superseded_by uuid REFERENCES source_version(id),
  raw_manifest_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL CHECK (status IN ('published','draft','withdrawn')),
  CHECK (effective_to IS NULL OR effective_to >= effective_from),
  UNIQUE(source_id, version_label)
);

CREATE TABLE evidence_span (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_version_id uuid NOT NULL REFERENCES source_version(id),
  artifact_id uuid REFERENCES artifact(id),
  locator_json jsonb NOT NULL,
  text_snippet text,
  extracted_at timestamptz NOT NULL DEFAULT now(),
  extractor_method text NOT NULL CHECK (extractor_method IN ('structured-parser','llm','human')),
  extraction_confidence numeric CHECK (extraction_confidence BETWEEN 0 AND 1)
);

CREATE TABLE assertion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_version_id uuid NOT NULL REFERENCES source_version(id),
  evidence_span_id uuid NOT NULL REFERENCES evidence_span(id),
  natural_language text NOT NULL,
  structured_payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  asserted_at timestamptz NOT NULL DEFAULT now(),
  interpreter_method text NOT NULL CHECK (interpreter_method IN ('structured-parser','llm','human')),
  interpretation_confidence numeric CHECK (interpretation_confidence BETWEEN 0 AND 1),
  review_status text NOT NULL CHECK (review_status IN ('unreviewed','reviewed','contested'))
);

CREATE TABLE assertion_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assertion_id uuid NOT NULL REFERENCES assertion(id),
  version_label text NOT NULL,
  natural_language text NOT NULL,
  structured_payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  valid_from date NOT NULL,
  valid_to date,
  superseded_by uuid REFERENCES assertion_version(id),
  CHECK (valid_to IS NULL OR valid_to >= valid_from),
  UNIQUE(assertion_id, version_label)
);

CREATE TABLE rule (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_version_id uuid NOT NULL REFERENCES source_version(id),
  title text NOT NULL,
  jurisdiction_scope_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  applicability_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  exceptions_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  delegated_to_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE rule_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id uuid NOT NULL REFERENCES rule(id),
  version_label text NOT NULL,
  logic_expr jsonb NOT NULL,
  valid_from date NOT NULL,
  valid_to date,
  superseded_by uuid REFERENCES rule_version(id),
  authority_class text NOT NULL CHECK (authority_class IN ('statutory','regulatory','administrative','ordinance','guideline','standard')),
  binding boolean NOT NULL DEFAULT true,
  CHECK (valid_to IS NULL OR valid_to >= valid_from),
  UNIQUE(rule_id, version_label)
);

CREATE TABLE applicability (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_version_id uuid NOT NULL REFERENCES rule_version(id),
  jurisdiction_id uuid NOT NULL REFERENCES jurisdiction(id),
  condition_expr jsonb NOT NULL,
  priority integer NOT NULL DEFAULT 0,
  UNIQUE(rule_version_id, jurisdiction_id, priority)
);

CREATE TABLE ontology_object (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_type text NOT NULL,
  external_refs_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE object_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id uuid NOT NULL REFERENCES ontology_object(id),
  version_label text NOT NULL,
  properties_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  geometry_refs_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  valid_from date NOT NULL,
  valid_to date,
  CHECK (valid_to IS NULL OR valid_to >= valid_from),
  UNIQUE(object_id, version_label)
);

CREATE TABLE relation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relation_type text NOT NULL,
  from_object_id uuid NOT NULL REFERENCES ontology_object(id),
  to_object_id uuid NOT NULL REFERENCES ontology_object(id),
  properties_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  valid_from date NOT NULL,
  valid_to date,
  CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE hyperedge (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hyperedge_type text NOT NULL,
  properties_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hyperedge_member (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hyperedge_id uuid NOT NULL REFERENCES hyperedge(id) ON DELETE CASCADE,
  role text NOT NULL,
  ref_type text NOT NULL,
  ref_id text NOT NULL,
  ordinal integer NOT NULL DEFAULT 0,
  UNIQUE(hyperedge_id, role, ref_type, ref_id)
);

CREATE TABLE evaluation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_version_id uuid NOT NULL REFERENCES rule_version(id),
  object_version_id uuid REFERENCES object_version(id),
  inputs_json jsonb NOT NULL,
  result_json jsonb NOT NULL,
  evaluated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE decision (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_id uuid NOT NULL UNIQUE REFERENCES evaluation(id),
  outcome text NOT NULL CHECK (outcome IN ('PASS','FAIL','REVIEW')),
  rationale_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  decided_at timestamptz NOT NULL DEFAULT now(),
  reviewer_id text
);

CREATE TABLE revision (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id text NOT NULL,
  label text NOT NULL,
  baseline_ref text NOT NULL,
  current_ref text NOT NULL,
  diff_manifest_uri text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_type text NOT NULL,
  target_refs_json jsonb NOT NULL,
  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  proposed_output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL CHECK (status IN ('proposed','approved','running','succeeded','failed','rejected')),
  requires_approval boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_id uuid NOT NULL REFERENCES action(id),
  executed_at timestamptz NOT NULL DEFAULT now(),
  result_json jsonb,
  error_json jsonb
);

CREATE TABLE agent_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id text NOT NULL,
  goal text NOT NULL,
  plan_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  result_json jsonb
);

CREATE TABLE quality_flag (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  target_table text NOT NULL,
  target_id uuid,
  flag_type text NOT NULL CHECK (flag_type IN ('conflict','ambiguity','missing_evidence','stale','contract_violation')),
  severity text NOT NULL DEFAULT 'medium' CHECK (severity IN ('low','medium','high','critical')),
  message text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  actor text NOT NULL,
  action text NOT NULL,
  target_table text NOT NULL,
  target_id text NOT NULL,
  before_json jsonb,
  after_json jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE domain_event (
  sequence bigserial PRIMARY KEY,
  event_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  event_type text NOT NULL,
  payload_json jsonb NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox_message (
  id bigserial PRIMARY KEY,
  event_id uuid NOT NULL REFERENCES domain_event(event_id),
  topic text NOT NULL,
  payload_json jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','published','failed')),
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);

CREATE TABLE projection_checkpoint (
  projection_name text PRIMARY KEY,
  last_sequence bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE embedding_projection (
  projection_key text PRIMARY KEY,
  source_type text NOT NULL,
  source_id text NOT NULL,
  content text NOT NULL,
  embedding vector(1536),
  canonical_updated_at timestamptz NOT NULL,
  projected_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_source_version_effective ON source_version(source_id, effective_from DESC);
CREATE INDEX ix_evidence_source_method ON evidence_span(source_version_id, extractor_method);
CREATE INDEX ix_assertion_review ON assertion(source_version_id, review_status, interpretation_confidence DESC NULLS LAST);
CREATE INDEX ix_rule_version_time ON rule_version(authority_class, binding, valid_from DESC);
CREATE INDEX ix_object_version_time ON object_version(object_id, valid_from DESC);
CREATE INDEX ix_relation_from ON relation(from_object_id, relation_type);
CREATE INDEX ix_relation_to ON relation(to_object_id, relation_type);
CREATE INDEX ix_hyperedge_type ON hyperedge(hyperedge_type, created_at DESC);
CREATE INDEX ix_hyperedge_member_ref ON hyperedge_member(ref_type, ref_id, role);
CREATE INDEX ix_applicability_jurisdiction ON applicability(rule_version_id, jurisdiction_id, priority DESC);
CREATE INDEX ix_event_aggregate ON domain_event(aggregate_type, aggregate_id, sequence);
CREATE INDEX ix_outbox_pending ON outbox_message(status, id) WHERE status = 'pending';

CREATE INDEX gin_evidence_locator ON evidence_span USING gin(locator_json);
CREATE INDEX gin_assertion_payload ON assertion USING gin(structured_payload_json);
CREATE INDEX gin_rule_logic ON rule_version USING gin(logic_expr);
CREATE INDEX gin_object_properties ON object_version USING gin(properties_json);
CREATE INDEX gin_action_targets ON action USING gin(target_refs_json);

COMMIT;
