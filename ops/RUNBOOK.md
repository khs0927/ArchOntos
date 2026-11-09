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

## 메트릭과 구조화 로그 (Metrics and audit logs)

`ARCHONTOS_LOG_FORMAT=json` 이면 로그 한 줄이 JSON 객체 하나입니다 (`ARCHONTOS_LOG_LEVEL`로 수준 조정).

- 요청 로그 (`archontos.request`): `request_id`, `method`, `route`(경로 템플릿, 실제 경로 아님),
  `status`, `duration_ms`, `actor`, `authenticated`. `X-Request-ID`는 `[A-Za-z0-9._-]{8,64}`일 때만
  그대로 전파하고, 아니면 새로 생성하며 응답 헤더로 돌려줍니다.
- 감사 이벤트 (`archontos.audit`, WARNING): `authentication_failed`, `authorization_denied`,
  `four_eyes_violation` — `actor`, `permission`, `route`, `outcome`, `reason`, `request_id`.
  DB `audit_log` 테이블과는 별개이며, 로그 수집기(예: Loki)로 보관/알림하세요.
- `Authorization`/`X-API-Key` 값과 토큰은 어떤 로그에도 기록되지 않습니다.
- Prometheus (`/metrics`): `archontos_http_request_duration_seconds{service,route,method,status_class}`,
  `archontos_http_requests_in_flight{service}`,
  `archontos_auth_failures_total{service,reason}` (reason: missing_key, invalid_key,
  unauthenticated, forbidden, four_eyes). 모든 라벨은 유한 집합입니다.
- 알림 예: `rate(archontos_auth_failures_total[5m]) > 1` (무차별 대입/오설정),
  `histogram_quantile(0.95, sum by (le, service) (rate(archontos_http_request_duration_seconds_bucket[5m])))`.

## 벡터 인덱스 (HNSW)

- Query time: `ARCHONTOS_HNSW_EF_SEARCH` (raised automatically to cover offset+limit),
  `ARCHONTOS_HNSW_ITERATIVE_SCAN` (`strict_order` default), `ARCHONTOS_HNSW_MAX_SCAN_TUPLES`.
- Build time: `python -m archontos.db.vector_index --m 24 --ef-construction 128` rebuilds the
  index `CONCURRENTLY` and swaps it in; `--show` prints the current parameters. Run it as the migrator.
