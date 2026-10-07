# Changelog

## Unreleased

- Least-privilege DB role: migration 012 creates the `archontos_app` group (DML only; on
  `audit_log` only SELECT/INSERT). `python -m archontos.db.roles ensure-login|check` provisions
  and verifies the service login. Compose (`db-login` service), CI (from the built image) and
  Helm use it, and services check at startup (`ARCHONTOS_DB_PRIVILEGE_CHECK`, `enforce` in
  compose/Helm). The whole integration suite now runs as a non-superuser.
- Jurisdiction RLS (migration 013, from the template) on `source_document`, fail-closed. Every
  transaction sets `app.actor` and `app.allowed_jurisdictions` (`ContextSession`).
- Authorization: `ARCHONTOS_ACTOR_ROLES` (viewer/proposer/approver/executor/curator/reviewer/
  operator/admin) guards every `/v1` route (a test enforces this); four-eyes on approval;
  `ARCHONTOS_ACTOR_JURISDICTIONS` for per-actor RLS scope.
- Search: opaque, query-bound cursor pagination (`next_cursor`, depth ≤ 1000); `min_score` is
  applied in SQL; `ARCHONTOS_HNSW_EF_SEARCH|ITERATIVE_SCAN|MAX_SCAN_TUPLES`;
  `python -m archontos.db.vector_index` rebuilds HNSW with `m`/`ef_construction` concurrently.
- Similarity search: `POST /v1/search/similar` (projection service) embeds the query with the
  configured embedder and ranks rows by cosine similarity, only against vectors from the same
  `embedding_model`; optional `source_type` and `min_score`. Migration 010 adds a partial HNSW
  index (`vector_cosine_ops`); queries enable pgvector iterative scans (`strict_order`).
- Identity (P3 groundwork): `ARCHONTOS_API_KEYS` accepts `actor:key`; requests run as that actor
  (echoed in `X-Archontos-Actor`); open mode trusts `X-Actor` as a hint. A named key cannot act as
  another actor (403). The actor is set per transaction as `app.actor`; migration 011 adds
  `action.created_by`, defaults `audit_log.actor` from `app.actor`, and makes `audit_log`
  append-only with forced RLS (SELECT/INSERT policies only).
- Embeddings: `ARCHONTOS_EMBEDDER=none|hashing|fastembed`. fastembed (Apache-2.0, ONNX, no
  torch) is an optional extra `archontos[embeddings]`; default model is multilingual
  (`paraphrase-multilingual-MiniLM-L12-v2`). Vectors are L2-normalised and zero-padded to
  `vector(1536)`; migration 009 adds `embedding_model` (NULL iff `embedding` is NULL).
- `AecSubjectRef.schema` is now `schema_id` (alias `schema`); the JSON contract is unchanged and
  the pydantic shadowing warning is gone. pydantic lower bound raised to 2.11.
- mypy (`check_untyped_defs`) is clean and runs in CI; fixed 8 typing errors.
- Helm: per-service HorizontalPodAutoscaler, resource requests/limits, NetworkPolicy
  `extraEgress` for PostgreSQL/object storage/law.go.kr.
- CI: `ruff format --check`, mypy, image import check, fastembed job, helm lint/render job.
- Persist report proposals and enforce the approval gate before `action_run`.
- Drain `domain_event` into `embedding_projection` with a rebuildable checkpoint.
- Increment `archontos_http_requests_total`. Optional `ARCHONTOS_API_KEYS`.
- Hyperedge create path and Helm ServiceAccount / NetworkPolicy skeleton.


## 2026-09-25
- ArchOntos 현재 상태를 기준으로 업그레이드 패키지 작성
- MVP-0 우선순위와 방향 수정 반영
- 기술 확장보다 provenance·normalization·deterministic decision을 우선하도록 정리
- P0 structured law evidence normalizer 및 evidence identity/persistence 구현
- P1 assertion candidate/review workflow 및 provenance composite FK 구현
- P2 approved-only safe rule compiler와 rule lifecycle(active/suspended) 구현
- P3 canonical query executors 및 5종 query API 구현
- source_version effective interval/superseded_by 자동 유지 추가
- 남은 release gate를 PostgreSQL 실제 migration + MVP-0 Golden Scenario E2E로 축소
