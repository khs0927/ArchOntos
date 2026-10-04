-- Production RLS template. Apply only after the application gateway sets session attributes.
-- Example per request:
--   SET LOCAL app.allowed_jurisdictions = 'KR,KR-11';
--   SET LOCAL app.role = 'engineer';
--
-- Kept OUT of db/migrations on purpose: docker-compose mounts that whole folder into initdb, and
-- enabling a policy before identity propagation exists would lock out legitimate users.

BEGIN;

ALTER TABLE source_document ENABLE ROW LEVEL SECURITY;

CREATE POLICY source_document_jurisdiction_read ON source_document
FOR SELECT
USING (
  jurisdiction_code = ANY (
    string_to_array(current_setting('app.allowed_jurisdictions', true), ',')
  )
);

COMMIT;
