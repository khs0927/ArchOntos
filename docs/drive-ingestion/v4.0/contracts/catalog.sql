-- Reference contract, SQLite 3.38+ (JSON functions). Not a production migration.
-- Every connection MUST execute PRAGMA foreign_keys=ON before transactions.
PRAGMA foreign_keys = ON;
CREATE TABLE sources (
 source_id TEXT PRIMARY KEY NOT NULL,
 provider TEXT NOT NULL CHECK(provider IN ('GOOGLE_DRIVE','LOCAL_FIXTURE')),
 account_namespace TEXT NOT NULL,
 UNIQUE(provider,account_namespace)
);
CREATE TABLE corpora (
 corpus_id TEXT PRIMARY KEY NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 corpus_key TEXT NOT NULL,
 UNIQUE(source_id,corpus_key),
 UNIQUE(corpus_id,source_id)
);
CREATE TABLE artifacts (
 artifact_id TEXT PRIMARY KEY NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 external_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('FILE','FOLDER','SHORTCUT','ARCHIVE_MEMBER','DERIVATIVE')),
 name TEXT NOT NULL,
 mime_type TEXT,
 access_state TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(access_state IN ('ACCESSIBLE','INACCESSIBLE','REMOVED_OR_INACCESSIBLE','UNKNOWN')),
 relevance TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED' CHECK(relevance IN ('ARCHITECTURE','SUPPORTING','UNRELATED_METADATA_ONLY','REVIEW_REQUIRED')),
 observed_at TEXT NOT NULL,
 UNIQUE(source_id,external_id),
 UNIQUE(artifact_id,source_id)
);
CREATE TABLE metadata_observations (
 observation_id TEXT PRIMARY KEY NOT NULL,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 observed_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('METADATA_ONLY','INACCESSIBLE','UNSTABLE')),
 metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object')
);
CREATE TABLE artifact_corpus_observations (
 artifact_id TEXT NOT NULL,
 corpus_id TEXT NOT NULL,
 source_id TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 PRIMARY KEY(artifact_id,corpus_id,observed_at),
 FOREIGN KEY(artifact_id,source_id) REFERENCES artifacts(artifact_id,source_id),
 FOREIGN KEY(corpus_id,source_id) REFERENCES corpora(corpus_id,source_id)
);
CREATE TABLE snapshots (
 snapshot_id TEXT PRIMARY KEY NOT NULL,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version_key TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 representation TEXT NOT NULL,
 capture_state TEXT NOT NULL CHECK(capture_state='CAPTURED'),
 bytes_uri TEXT NOT NULL CHECK(length(bytes_uri)>0),
 byte_size INTEGER NOT NULL CHECK(byte_size>=0),
 captured_at TEXT NOT NULL,
 UNIQUE(artifact_id,version_key,representation)
);
-- Archive identity includes container snapshot + member ordinal, not path alone.
CREATE TABLE archive_members (
 artifact_id TEXT PRIMARY KEY NOT NULL REFERENCES artifacts(artifact_id),
 container_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
 member_ordinal INTEGER NOT NULL CHECK(member_ordinal>=0),
 original_path TEXT NOT NULL,
 normalized_path TEXT NOT NULL,
 UNIQUE(container_snapshot_id,member_ordinal)
);
CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
 stage TEXT NOT NULL,
 profile_id TEXT NOT NULL,
 profile_version TEXT NOT NULL,
 parser_id TEXT NOT NULL,
 parser_version TEXT NOT NULL,
 config_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','RUNNING','COMPLETE','PARTIAL','UNSUPPORTED','INACCESSIBLE','ERROR_RETRYABLE','ERROR_FINAL','REVIEW_REQUIRED')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 lease_owner TEXT,
 lease_until TEXT,
 UNIQUE(snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash),
 UNIQUE(job_id,snapshot_id),
 CHECK(status<>'RUNNING' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE TABLE evidence (
 evidence_id TEXT PRIMARY KEY NOT NULL,
 job_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL,
 locator_json TEXT NOT NULL CHECK(json_valid(locator_json) AND json_type(locator_json)='object'),
 confidence REAL CHECK(confidence BETWEEN 0 AND 1),
 FOREIGN KEY(job_id,snapshot_id) REFERENCES jobs(job_id,snapshot_id),
 UNIQUE(evidence_id,job_id)
);
CREATE TABLE assertions (
 assertion_id TEXT PRIMARY KEY NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 evidence_id TEXT NOT NULL,
 subject_uri TEXT NOT NULL,
 predicate_uri TEXT NOT NULL CHECK(predicate_uri NOT IN ('owl:sameAs','http://www.w3.org/2002/07/owl#sameAs')),
 object_json TEXT NOT NULL CHECK(json_valid(object_json)),
 review_state TEXT NOT NULL DEFAULT 'PROVISIONAL' CHECK(review_state IN ('PROVISIONAL','ACCEPTED','REJECTED','STALE')),
 reviewer TEXT,
 reviewed_at TEXT,
 FOREIGN KEY(evidence_id,job_id) REFERENCES evidence(evidence_id,job_id),
 CHECK(review_state IN ('PROVISIONAL','STALE') OR (reviewer IS NOT NULL AND reviewed_at IS NOT NULL))
);
-- Receipt + observations/jobs + cursor advancement must commit in ONE transaction.
-- Apply compare-and-swap: UPDATE cursors ... WHERE committed_token=:old_token.
CREATE TABLE change_cursors (
 corpus_id TEXT PRIMARY KEY NOT NULL REFERENCES corpora(corpus_id),
 committed_token TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE change_page_receipts (
 corpus_id TEXT NOT NULL REFERENCES corpora(corpus_id),
 input_token TEXT NOT NULL,
 output_token TEXT NOT NULL,
 committed_at TEXT NOT NULL,
 PRIMARY KEY(corpus_id,input_token)
);
CREATE INDEX jobs_status_idx ON jobs(status,lease_until);
CREATE INDEX snapshots_hash_idx ON snapshots(sha256);
