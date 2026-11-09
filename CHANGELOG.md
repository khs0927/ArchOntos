# Changelog

## Unreleased

- MVP-3 revision diff (docs/MVP3-REVISION-DIFF.md):
  - `archontos.cad.diff` matches `arch_object` rows of two revisions by DXF handle, then by
    centroid distance and bbox IoU, and classifies added/removed/modified/unchanged with
    per-property deltas (units included) under configurable tolerances.
  - Impact analysis links changes to MVP-2 check results. Results of the base revision are
    marked stale and never carried over. `rerun_checks` re-runs them on the target revision and
    reports outcome transitions (PASS to FAIL and so on).
  - Migration 017 adds `cad_revision_diff` and `cad_object_change`: append-only, jurisdiction
    RLS, `archontos_app` SELECT/INSERT only.
  - New endpoints `POST /v1/cad/diffs`, `GET /v1/cad/diffs/{id}` and `.../changes`.
    Check runs and results gain `stale` and `stale_by_diff_ids`.
  - New permission `cad.diff` (curator, reviewer, admin). `rerun_checks` also needs `cad.check`.
  - New event `CadRevisionDiffed` on outbox topic `cad.revision-diffed`.

- MVP-2 regulation-to-CAD checks (docs/MVP2-CAD-CHECKS.md):
  - `archontos.cad.checks` extracts rule DSL facts from `arch_object`: lengths in m, areas in
    m2, `fact_units`, and provenance by object id and DXF handle.
  - Binding takes only active rule versions with approved basis that are effective on the
    reference date and whose jurisdiction covers the run. Land-parcel facts are optional.
  - Rules are evaluated per object or per drawing and fail closed: a missing fact, a unit
    mismatch or an unexecutable rule gives REVIEW.
  - Migration 015 adds `cad_check_run` and `cad_check_result`: append-only, jurisdiction RLS,
    `archontos_app` SELECT/INSERT only.
  - New endpoints `POST/GET /v1/cad/revisions/{id}/checks` and `GET .../checks/results`, with
    filters and cursors.
  - New permission `cad.check` (curator, reviewer).
  - New event `CadChecksCompleted` on outbox topic `cad.checks-completed`.
- MVP-1 DXF/CAD object normalization (docs/MVP1-CAD.md): migration 014 (`cad_artifact`,
  `cad_revision`, `cad_entity`, `arch_object`, `arch_object_source`; append-only triggers,
  jurisdiction RLS, `archontos_app` SELECT/INSERT only); deterministic ezdxf parser (optional
  `cad` extra, installed in CI and the image); versioned layer/block mapping rules with Korean and
  AIA layer names; `/v1/cad/artifacts` upload and paginated revision/object/entity/facts reads;
  permissions `cad.read` (all roles) and `cad.ingest` (curator); `CadRevisionIngested` event on
  outbox topic `cad.revision-ingested`; artifact stores gain `put_bytes`.
- Packaging: the wheel now ships `db/migrations/*.sql` as `archontos._migrations`, and
  `default_migrations_dir()` falls back to it (`packaged_migrations_dir()`), so a git/wheel install
  can run `python -m archontos.db.migrate` without a source checkout.
- Shared-database schema option: `ARCHONTOS_DB_SCHEMA` (settings `db_schema`) puts every
  ArchOntos table in that PostgreSQL schema (search_path `<schema>, public`, so pgvector types in
  `public` still resolve); `python -m archontos.db.migrate --schema` creates and migrates it.
  Upstreamed from the Ontology-platform monorepo, which consumes ArchOntos as a dependency.

## 0.1.0-rc1

MVP-0 release candidate: hardening on top of the entries below.

- Optional OIDC/JWT identity (`ARCHONTOS_OIDC_ISSUER|AUDIENCE|JWKS_URL`; all three or none).
  `Authorization: Bearer` tokens are verified (RS256 only; signature, `exp`, `iss`, `aud`) with
  PyJWT's JWKS client and become the same `Principal` as an API key: actor from
  `ARCHONTOS_OIDC_ACTOR_CLAIM` (`sub`), roles from `ARCHONTOS_OIDC_ROLES_CLAIM` (`roles`, dotted
  paths such as `realm_access.roles`), jurisdictions from `ARCHONTOS_OIDC_JURISDICTIONS_CLAIM`;
  `ARCHONTOS_ACTOR_ROLES` / `_JURISDICTIONS` entries for the same actor are merged in. API keys
  keep working side by side. With OIDC on and no API keys, anonymous requests get 401 +
  `WWW-Authenticate: Bearer`. Disabled by default. New dependency `PyJWT[crypto]`.
- Structured request logging: one JSON line per non-health request on stdout (`event`,
  `request_id`, `actor`, `authenticated`, `auth`, `method`, `route` template, `status`,
  `duration_ms`); `X-Request-Id` is accepted (validated) or generated and echoed.
  `ARCHONTOS_LOG_FORMAT=json|text|off`, `ARCHONTOS_LOG_LEVEL`. Secrets are never logged.
- Backup/restore: `ops/backup.sh` (pg_dump -Fc + sha256 + per-table row counts),
  `ops/restore.sh` (checksum, refuses non-empty target without `--clean`, verifies counts),
  `ops/backup-restore-check.sh` and a CI step running the round trip on PostgreSQL 18.
  RUNBOOK documents backup before migration and the rollback procedure.

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

## Unreleased: MVP-4 IFC / SketchUp mapping

- MVP-4 model import (docs/MVP4-IFC-SKETCHUP.md):
  - `POST /v1/cad/artifacts` detects and parses IFC (`archontos.cad.ifc`, optional `bim` extra
    with ifcopenshell) and SketchUp JSON `archontos-skp-export/1` (`archontos.cad.skp`,
    validated with `jsonschema`, now a core dependency). Binary `.skp` returns 415.
  - Versioned model mapping rules `model-2026.11` map IFC classes and SketchUp tags or
    definitions to `arch_object` kinds. The new `slab` kind is added. Quantities from
    attributes, Qto sets and Psets are normalised to m, m2 and m3 with `fact_units`.
  - Migration 018 adds `source_format` to `cad_artifact` and `cad_revision`, plus
    `cad_revision.model_meta_json`, the `slab` kind, `arch_object_source_ref` and
    `model_object_link` (append-only, jurisdiction RLS).
  - `building.floor_count`, `basement_count` and `storey_count` come from IFC storeys. They
    appear in revision facts and MVP-2 check runs.
  - New endpoints `POST /v1/models/links` (new permission `cad.link`) and
    `GET /v1/models/links`.
