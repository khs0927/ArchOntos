BEGIN;

ALTER TABLE outbox_message
  ADD COLUMN IF NOT EXISTS last_error text;

ALTER TABLE outbox_message
  ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz;

COMMIT;
