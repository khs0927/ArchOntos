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

## 외부 IdP (OIDC)

`ARCHONTOS_OIDC_ISSUER`와 `ARCHONTOS_OIDC_AUDIENCE`를 모두 설정하면 `Authorization: Bearer <JWT>`를
검증합니다 (Keycloak, Entra ID, Auth0 등 JWKS를 제공하는 IdP). 서명은 `ARCHONTOS_OIDC_JWKS_URL`
(기본 `<issuer>/.well-known/jwks.json`)에서 가져온 키로 검증하며 `iss`·`aud`·`exp`가 필수입니다.

- 알고리즘은 설정(`ARCHONTOS_OIDC_ALGORITHMS`, 기본 `RS256,ES256`)으로 고정되고 비대칭만 허용합니다.
  토큰 헤더의 `alg`는 신뢰하지 않으며 `none`·HMAC은 거절합니다.
- 행위자는 `ARCHONTOS_OIDC_ACTOR_CLAIM`(기본 `preferred_username`, 없으면 `sub`)이며 actor 규칙
  `[A-Za-z0-9._@-]{1,128}`을 따라야 합니다.
- 역할은 `ARCHONTOS_OIDC_ROLES_CLAIM`(기본 `roles`)에서 읽고 위 표에 없는 값은 버립니다.
  관할은 `ARCHONTOS_OIDC_JURISDICTIONS_CLAIM`을 설정했을 때만 토큰에서 가져옵니다.
- OIDC가 켜지면 모든 `/v1` 경로에 권한 검사가 적용되고 `X-Actor` 힌트는 무시됩니다.
  잘못된 Bearer 토큰은 401이며 API 키로 대체 인증하지 않습니다. IdP 키를 가져올 수 없으면 거절(fail-closed).
- 알 수 없는 `kid`로 JWKS를 다시 가져오는 간격은 30초 이상입니다.

## 벡터 인덱스 (HNSW)

- Query time: `ARCHONTOS_HNSW_EF_SEARCH` (raised automatically to cover offset+limit),
  `ARCHONTOS_HNSW_ITERATIVE_SCAN` (`strict_order` default), `ARCHONTOS_HNSW_MAX_SCAN_TUPLES`.
- Build time: `python -m archontos.db.vector_index --m 24 --ef-construction 128` rebuilds the
  index `CONCURRENTLY` and swaps it in; `--show` prints the current parameters. Run it as the migrator.
