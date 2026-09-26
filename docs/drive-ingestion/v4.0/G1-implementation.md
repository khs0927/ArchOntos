# G1 Drive metadata inventory — implementation checkpoint

This adds a read-only Google Drive v3 metadata adapter, resumable page
orchestration, and a PostgreSQL provider layer. It is a first implementation
checkpoint, not an accepted full-Drive inventory or a document parser.

## Data boundary

- `drive_provider_account` identifies an account by a stable, opaque namespace;
  `drive_corpus` tracks the user and each accessible shared drive separately.
- `drive_item` identifies the provider file ID regardless of folder moves.
  Page receipts, metadata observations, membership, and change cursors remain
  separate. The same file may be observed in multiple corpora.
- The existing `source_document`, `source_version`, `evidence_span`, and
  `assertion` tables retain their law-specific meaning. Inventory observations
  never insert into `artifact` because metadata is not captured file bytes.
- Removed/trashed/shared-drive-loss events block matching corpus memberships
  and emit a domain event and outbox message in the same transaction.
  A future search/preview service must enforce this boundary and invalidate
  derivative caches before any content becomes visible.

## Run in a controlled environment

1. Apply `db/migrations/008_drive_inventory.sql` to an existing ArchOntos
   PostgreSQL database after its initial schema. Existing database volumes do
   not apply new SQL migrations automatically. Back up and verify migration
   execution before running the scanner.
2. Install the repository (`pip install -e '.[dev]'`) and supply an OAuth access
   token with an appropriate Drive read scope through the environment variable
   `ARCHONTOS_DRIVE_ACCESS_TOKEN`. OAuth consent, refresh, and scope verification
   are not implemented by this checkpoint. Set the normal ArchOntos database
   URL through its existing configuration.
3. Run `python apps/drive_inventory.py --account-namespace <opaque-stable-id>`.
   The namespace must be derived consistently from the authorized provider
   account, never from a folder path. The command reports counts without file
   names, IDs, or tokens.

The scanner captures the user change token before shared-drive discovery and
captures each discovered drive's token before any baseline listing. It commits
every file page atomically, follows empty pages with a next token, rejects
`incompleteSearch`, and replays changes from the captured token. A failed API
page leaves its checkpoint unchanged; rerunning the command resumes it. Shared
drive removals are represented without inventing a file ID.

## Verification and remaining gates

The fake HTTP and fake database tests cover page traversal, failure/resume,
receipt idempotence, cursor rollback, removal blocking and matching outbox
events. Run `pytest tests/test_drive_adapter.py tests/test_drive_service.py
tests/test_drive_persistence.py` and `ruff check .` with project dependencies.
The database tests use a fake session; run the SQL against a real PostgreSQL
instance and verify the applied constraints, concurrent workers, and event
visibility before an operational rollout.

No live OAuth token is supplied by the ChatGPT Drive connector to this code.
Its limited file listing cannot substitute for `changes.list` or a complete
Drive v3 inventory. The CLI has no token refresh or 429/backoff handler: renew
an expired token and rerun from the saved checkpoint. Rejected/expired baseline
page tokens require an explicit new scan generation; silently continuing would
claim a complete corpus without evidence. New shared drives require periodic
rediscovery. Full content capture, CAD/HWP/SKP parsing, ACL-safe search and
preview, backup restoration, and real Drive acceptance gates remain open.
