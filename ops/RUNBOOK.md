# Operations Runbook

## 장애 우선순위

1. canonical DB 쓰기 실패: ingestion 중단, 재시도
2. artifact 저장 실패: source_version commit 금지
3. outbox 지연: canonical state는 보존하고 worker 재시작
4. projection 오류: projection을 격리하고 rebuild
5. rule 오류: 해당 rule version을 비활성화하고 REVIEW로 전환

## 원칙

파생 시스템의 장애는 canonical state를 수정하지 않는다. conflict는 rollback으로 사라지지 않도록 별도 commit 경계를 유지한다.

## 복구 후 확인

- artifact hash
- source version count
- outbox pending count
- projection checkpoint
- 최근 decision provenance

## 데이터베이스 역할과 RLS (Database roles and row-level security)

Services must never connect as a superuser, a `BYPASSRLS` role, or the table owner: all
three skip row-level security (migrations 011 and 013).

| Role | Used by | Privileges |
| --- | --- | --- |
| migrator (owner, e.g. `archontos`) | `python -m archontos.db.migrate`, `db.roles ensure-login`, `db.vector_index` | owns the schema; needs `CREATEROLE` for migration 012 |
| `archontos_app` (group, NOLOGIN) | created by migration 012 | DML on all tables; on `audit_log` only SELECT/INSERT; on `schema_migrations` only SELECT |
| service login (e.g. `archontos_svc`) | every service `ARCHONTOS_DATABASE_URL` | member of `archontos_app`, nothing else |

Provision or rotate the service login (the password comes from an environment variable, never argv):

```bash
ARCHONTOS_APP_DB_PASSWORD=... python -m archontos.db.roles ensure-login --name archontos_svc --dsn "$MIGRATOR_DSN"
python -m archontos.db.roles check --dsn "$SERVICE_DSN"   # exit 1 if it can bypass RLS
```

Compose does this in the `db-login` one-shot service. Services run the same check at startup
(`ARCHONTOS_DB_PRIVILEGE_CHECK`: `off`/`warn`/`enforce`; compose and Helm use `enforce`).

Each transaction carries `app.actor` and `app.allowed_jurisdictions` (set transaction-locally
by `archontos.db.session.ContextSession`):

- `audit_log`: append-only (forced RLS with SELECT/INSERT policies only, plus no UPDATE/DELETE grant).
- `source_document`: rows outside `app.allowed_jurisdictions` are invisible and cannot be
  written. An unset or empty value matches nothing (fail-closed). The default comes from
  `ARCHONTOS_ALLOWED_JURISDICTIONS`, or per actor from `ARCHONTOS_ACTOR_JURISDICTIONS`.

## 권한 (Authorization)

`ARCHONTOS_ACTOR_ROLES=alice:approver+executor,bob:proposer` turns authorization on. With it
set, every `/v1` route requires a permission. Only named API keys (`ARCHONTOS_API_KEYS=alice:<key>`)
are authenticated; anonymous callers get 401 and callers without the permission get 403.
The proposer of an action cannot approve it, even with `admin` (four-eyes).

| Role | Permissions |
| --- | --- |
| viewer | action.read, search.query, query.read |
| proposer | viewer + action.propose |
| approver | viewer + action.approve, action.reject |
| executor | viewer + action.execute |
| curator | viewer + source.ingest, assertion.propose |
| reviewer | viewer + assertion.review, rule.compile |
| operator | viewer + hyperedge.write, projection.admin |
| admin | everything |

## 벡터 인덱스 (HNSW)

- Query time: `ARCHONTOS_HNSW_EF_SEARCH` (raised automatically to cover offset+limit),
  `ARCHONTOS_HNSW_ITERATIVE_SCAN` (`strict_order` default), `ARCHONTOS_HNSW_MAX_SCAN_TUPLES`.
- Build time: `python -m archontos.db.vector_index --m 24 --ef-construction 128` rebuilds the
  index `CONCURRENTLY` and swaps it in; `--show` prints the current parameters. Run it as the migrator.


## 외부 ID 공급자 (OIDC/JWT, optional)

Off unless all of `ARCHONTOS_OIDC_ISSUER`, `ARCHONTOS_OIDC_AUDIENCE`, `ARCHONTOS_OIDC_JWKS_URL`
are set (a partial set stops the service at startup). Clients send `Authorization: Bearer <jwt>`;
only RS256 is accepted, and `exp`, `iss`, `aud` are required. Mapping:

- actor: `ARCHONTOS_OIDC_ACTOR_CLAIM` (default `sub`); must match `[A-Za-z0-9._@-]{1,128}`, so for
  opaque subjects such as `auth0|123` point it at `email` or a custom claim.
- roles: `ARCHONTOS_OIDC_ROLES_CLAIM` (default `roles`; array or space/comma string; dotted path
  such as `realm_access.roles`). Unknown role names are dropped, never granted.
- jurisdictions: `ARCHONTOS_OIDC_JURISDICTIONS_CLAIM` (default `jurisdictions`); when absent,
  `ARCHONTOS_ACTOR_JURISDICTIONS` or the deployment default applies.
- `ARCHONTOS_ACTOR_ROLES` entries for the same actor are added on top of the token's roles.
- Clock skew `ARCHONTOS_OIDC_LEEWAY_SECONDS` (30). JWKS keys are cached 5 minutes, so key rotation
  needs no restart. A 401 body never says why a token failed; the reason is in the JSON log
  (`event=auth_failed`). `X-API-Key` keeps working in parallel.

## 요청/감사 로그 (Structured logging)

Every non-`/health`, non-`/metrics` request writes one JSON line to stdout, for example
`{"ts":"...","level":"INFO","logger":"archontos.request","msg":"request","event":"http_request",
"request_id":"...","actor":"alice","auth":"oidc","method":"POST",
"route":"/v1/actions/{action_id}/approve","status":200,"duration_ms":4.2}`. `request_id` is the
caller's `X-Request-Id` (if it matches `[A-Za-z0-9._-]{1,64}`) or generated, and is echoed in the
response. The route is the template, never the raw path. Durable audit stays in the append-only
`audit_log` table; this log is for correlation and alerting.
`ARCHONTOS_LOG_FORMAT=json|text|off`, `ARCHONTOS_LOG_LEVEL=INFO`.

## 백업 / 복구 / 마이그레이션 롤백 (Backup, restore, migration rollback)

Migrations are forward-only (`db/migrations/NNN_*.sql`, checksummed in `schema_migrations`).
Rollback therefore means restoring the backup taken just before the upgrade.

**Backup** (run as the migrator/owner; password via `PGPASSWORD`/`~/.pgpass`, not argv):

```bash
ARCHONTOS_BACKUP_DSN=postgresql://archontos@db:5432/archontos ops/backup.sh /var/backups/archontos
```

Produces `archontos-<UTC>.dump` (pg_dump custom format), `.sha256` and `.counts.tsv` (exact row
count per table). `pg_dump` must be the same or a newer major version than the server. Object
storage (MinIO artifacts) is NOT in the dump: snapshot the bucket in the same window, because
`artifact` rows reference it by hash.

**Before every upgrade:** `ops/backup.sh`, then `python -m archontos.db.migrate --status`, then
`python -m archontos.db.migrate`.

**Restore** into a new (or emptied) database:

```bash
createdb -O archontos archontos_restored
ops/restore.sh archontos-<UTC>.dump postgresql://archontos@db:5432/archontos_restored
python -m archontos.db.roles ensure-login --name archontos_svc --dsn <migrator dsn>  # LOGIN role
python -m archontos.db.migrate --dsn <restored dsn> --status   # nothing may be pending
```

`restore.sh` verifies the checksum, refuses a database that already has tables (pass `--clean`
to drop and recreate the dumped objects), creates the `archontos_app` group role if missing
(roles live in the cluster, not in the dump), then compares every table's row count with
`.counts.tsv` (exit 4 on mismatch).

**Rolling back a bad migration:**

1. Stop the services (or scale to 0) so nothing writes.
2. Optional: `ops/backup.sh` the current state for forensics.
3. Restore the pre-upgrade dump into a fresh database with `restore.sh`, then point
   `ARCHONTOS_DATABASE_URL` at it (or `--clean` into the original database).
4. Deploy the previous application version (it matches the restored schema).
5. Run "복구 후 확인" above (artifact hash, source version count, outbox pending, projection
   checkpoint). Projection tables are derived: rebuild them rather than trusting them.
6. Writes made after the backup are lost; re-ingest from the original sources (ingestion is
   idempotent).

**CI/local check:**
`ARCHONTOS_TEST_DATABASE_URL=postgresql://u:p@host:5432/postgres ops/backup-restore-check.sh`
migrates a throwaway database, seeds rows, dumps, restores into another throwaway database and
fails on any row-count difference. CI runs it against PostgreSQL 18 (ci.yml). Rehearse a real
restore regularly: a backup that has not been restored is not a backup.
