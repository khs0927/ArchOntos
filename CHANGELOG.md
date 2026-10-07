# Changelog

## Unreleased

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
