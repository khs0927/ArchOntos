# Release Checklist

## Data integrity
- [ ] artifact SHA-256 검증
- [ ] 동일 MST/different bytes conflict test
- [ ] source version uniqueness 확인
- [ ] evidence locator round-trip 확인
- [ ] migration up/down 또는 rollback 절차 확인

## Rule safety
- [ ] unsupported operator fail-closed
- [ ] unreviewed assertion은 REVIEW
- [ ] effective date와 jurisdiction 필수
- [ ] 모든 decision에 provenance 존재

## Operations
- [ ] DB backup 및 restore rehearsal
- [ ] outbox retry/dead-letter 정책
- [ ] health/readiness endpoint
- [ ] structured logs와 correlation ID
- [ ] secret이 로그·artifact metadata에 노출되지 않음

## Security
- [ ] identity propagation 설계 검증
- [ ] RLS enforcement 전 integration test
- [ ] 최소권한 DB role
- [ ] action approval audit

## Release gate
- [ ] local tests green
- [ ] compileall green
- [ ] deterministic E2E green
- [ ] 대표 API response에 원문 근거 포함
