# Upgrade Plan

## 1. 현재 진단

현재 ArchOntos는 공식 law.go.kr 데이터를 immutable artifact와 canonical source version으로 저장하는 단계까지 구현되어 있다. 핵심 미완성 경계는 법령 구조를 evidence span으로 정규화하고, 검토된 assertion을 실행 가능한 rule로 연결하는 부분이다.

## 2. 방향 수정

### 유지
- Modular monorepo
- PostgreSQL canonical store
- Transactional outbox
- Official-source-first ingestion
- Rule DSL와 fail-closed 실행
- Human approval 기반 action

### 즉시 보류
- Kafka/NATS
- Neo4j/Jena 동시 도입
- GraphRAG 기반 PASS/FAIL
- Kubernetes production 전환
- CAD 자동 수정
- KPI/ROI 대시보드

### 수정
- `source_version → evidence_span`을 최우선 제품 경계로 둔다.
- 모든 법규 답변은 관할·시행일·authority·evidence locator를 필수로 반환한다.
- AI는 assertion 후보를 만들 수 있지만, 검토 전에는 rule이 될 수 없게 한다.
- projection은 canonical query와 E2E가 통과한 뒤 도입한다.

## 3. 단계별 목표

| 단계 | 목표 | 완료 기준 |
|---|---|---|
| P0 | 조/항/호/목/별표 normalizer | 대표 법령 fixture가 deterministic하게 span 생성 |
| P1 | EvidenceSpan persistence | 재실행 시 중복 없이 동일 ID·hash 유지 |
| P2 | Assertion workflow | 후보·검토·승인·반려 상태와 provenance 연결 |
| P3 | Rule compiler | 승인 assertion만 제한 DSL로 compile |
| P4 | Query executor | 5개 intent가 SQL로 실행되고 근거 반환 |
| P5 | MVP-0 E2E | 법령 ingest부터 decision 설명까지 통과 |
| P6 | Vector projection | rebuild 및 checkpoint 복구 검증 |
| M1 | DXF/CAD base | entity→architectural object와 revision 저장 |

## 4. 권장 작업 티켓

- NORM-001: 법령 조문 트리 parser
- NORM-002: 부칙·별표 locator 모델
- EVID-001: evidence span writer 및 idempotency
- ASSERT-001: assertion candidate schema
- ASSERT-002: human review API
- RULE-001: approved assertion compiler
- RULE-002: applicability resolver
- QUERY-001: source evidence executor
- QUERY-002: temporal comparison executor
- QUERY-003: jurisdiction comparison executor
- E2E-001: MVP-0 golden scenario
- OPS-001: migration rollback and backup runbook
- SEC-001: identity propagation before RLS enforcement

## 5. Definition of Done

- 원문 artifact hash와 locator로 모든 판정이 역추적된다.
- 동일 입력을 반복 처리해도 결과가 변하지 않는다.
- 불확실하거나 검토되지 않은 assertion은 REVIEW로 남는다.
- rule engine은 지원하지 않는 DSL을 거부한다.
- API 응답에 source version, effective date, jurisdiction, evidence가 포함된다.
- 실패 시 canonical state와 outbox의 일관성이 보장된다.
