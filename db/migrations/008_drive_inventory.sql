-- Google Drive inventory is metadata only. A content artifact/snapshot is created
-- separately, and only after the corresponding bytes have been captured.
-- Self-contained: every table it creates is drive_* and every foreign key
-- points at another drive_* table in this same file. It touches no table that
-- migrations 002 to 007 alter, so it is numbered 008 and runs last purely to
-- keep the 004 to 007 range unambiguous. It depends only on 001_initial.sql for
-- domain_event and outbox_message, and it is idempotent by construction.
CREATE TABLE IF NOT EXISTS drive_provider_account (
    id uuid PRIMARY KEY,
    provider text NOT NULL DEFAULT 'GOOGLE_DRIVE' CHECK (provider = 'GOOGLE_DRIVE'),
    account_namespace text NOT NULL UNIQUE CHECK (length(account_namespace) > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS drive_corpus (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES drive_provider_account(id),
    corpus_key text NOT NULL CHECK (length(corpus_key) > 0),
    start_token text NOT NULL CHECK (length(start_token) > 0),
    baseline_scan_id uuid NOT NULL,
    baseline_complete boolean NOT NULL DEFAULT false,
    baseline_page_token text,
    baseline_last_page_seen boolean NOT NULL DEFAULT false,
    change_token text NOT NULL CHECK (length(change_token) > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, corpus_key),
    CHECK (baseline_complete = false OR baseline_last_page_seen = true)
);

-- Identity is (provider account, Drive file ID), never a file name or path.
CREATE TABLE IF NOT EXISTS drive_item (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES drive_provider_account(id),
    provider_file_id text NOT NULL CHECK (length(provider_file_id) > 0),
    drive_id text,
    name text,
    mime_type text,
    modified_time timestamptz,
    checksum text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, provider_file_id)
);

CREATE TABLE IF NOT EXISTS drive_baseline_page_receipt (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL REFERENCES drive_corpus(id),
    scan_id uuid NOT NULL,
    input_page_token text NOT NULL, -- empty string represents the first page
    output_page_token text,
    page_fingerprint text NOT NULL,
    is_last boolean NOT NULL,
    item_count integer NOT NULL CHECK (item_count >= 0),
    committed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (corpus_id, scan_id, input_page_token),
    CHECK ((is_last AND output_page_token IS NULL) OR
           (NOT is_last AND output_page_token IS NOT NULL AND length(output_page_token) > 0))
);

CREATE TABLE IF NOT EXISTS drive_change_page_receipt (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL REFERENCES drive_corpus(id),
    input_change_token text NOT NULL,
    output_change_token text NOT NULL CHECK (length(output_change_token) > 0),
    page_fingerprint text NOT NULL,
    is_last boolean NOT NULL,
    change_count integer NOT NULL CHECK (change_count >= 0),
    committed_at timestamptz NOT NULL DEFAULT now(),
    -- A no-op terminal poll may return the same start token. Later changes
    -- legitimately reuse that input token with a different page fingerprint.
    UNIQUE (corpus_id, input_change_token, page_fingerprint)
);

CREATE TABLE IF NOT EXISTS drive_metadata_observation (
    id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES drive_item(id),
    item_ordinal integer NOT NULL CHECK (item_ordinal >= 0),
    baseline_receipt_id uuid REFERENCES drive_baseline_page_receipt(id),
    change_receipt_id uuid REFERENCES drive_change_page_receipt(id),
    state text NOT NULL CHECK (state IN ('METADATA_ONLY','REMOVED','INACCESSIBLE')),
    metadata_json jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((baseline_receipt_id IS NOT NULL) <> (change_receipt_id IS NOT NULL)),
    UNIQUE (baseline_receipt_id, item_ordinal),
    UNIQUE (change_receipt_id, item_ordinal)
);

CREATE TABLE IF NOT EXISTS drive_access_observation (
    id uuid PRIMARY KEY,
    change_receipt_id uuid NOT NULL REFERENCES drive_change_page_receipt(id),
    change_ordinal integer NOT NULL CHECK (change_ordinal >= 0),
    drive_id text NOT NULL CHECK (length(drive_id) > 0),
    removed boolean NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_receipt_id, change_ordinal)
);

-- Membership is scoped to a corpus. Absence in one corpus never deletes an
-- account-wide item or any membership in another corpus.
CREATE TABLE IF NOT EXISTS drive_corpus_membership (
    corpus_id uuid NOT NULL REFERENCES drive_corpus(id),
    item_id uuid NOT NULL REFERENCES drive_item(id),
    status text NOT NULL CHECK (status IN ('ACTIVE','BLOCKED')),
    reason text,
    last_seen_scan_id uuid,
    last_observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus_id, item_id),
    CHECK (status <> 'BLOCKED' OR reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS drive_item_account_idx ON drive_item(account_id);
CREATE INDEX IF NOT EXISTS drive_item_shared_drive_idx ON drive_item(account_id, drive_id);
CREATE INDEX IF NOT EXISTS drive_membership_reconciliation_idx
    ON drive_corpus_membership(corpus_id, status, last_seen_scan_id);
CREATE INDEX IF NOT EXISTS drive_metadata_observation_item_idx
    ON drive_metadata_observation(item_id, observed_at DESC);
