# Changelog

## Unreleased

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
