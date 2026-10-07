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
