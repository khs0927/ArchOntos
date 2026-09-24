-- Production RLS template. Apply only after the application gateway sets session attributes.
-- Example per request:
--   SET LOCAL app.allowed_jurisdictions = 'KR,KR-11';
--   SET LOCAL app.role = 'engineer';
--
-- This migration is intentionally NOT auto-mounted in docker-compose until identity propagation
-- is implemented. Enabling a policy before the gateway sets claims would lock out legitimate users.

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
