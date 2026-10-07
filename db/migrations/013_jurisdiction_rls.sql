BEGIN;

-- Jurisdiction row-level security (from db/templates/rls_policy_template.sql), now that every
-- application transaction sets app.allowed_jurisdictions (archontos.db.session.ContextSession).
-- Fail-closed: an unset or empty setting matches nothing. Not FORCEd: the migrator (table
-- owner) and maintenance jobs keep full access; the service login (archontos_app, migration
-- 012) is always subject to the policy. Dependent rows (source_version, evidence, ...) are
-- reached through source_document joins in the canonical queries.
ALTER TABLE source_document ENABLE ROW LEVEL SECURITY;

CREATE POLICY source_document_jurisdiction ON source_document
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
