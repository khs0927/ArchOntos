BEGIN;

-- Cross-repository subject identities remain JSON references; they are not local object foreign keys.
ALTER TABLE assertion
  ADD COLUMN applies_to_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD CONSTRAINT assertion_applies_to_array CHECK (jsonb_typeof(applies_to_json) = 'array'),
  ADD CONSTRAINT assertion_applies_to_bound CHECK (jsonb_array_length(applies_to_json) <= 10000);

CREATE INDEX gin_assertion_aec_subjects ON assertion USING gin(applies_to_json);

COMMIT;
