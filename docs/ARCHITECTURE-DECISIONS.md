# Architecture Decisions

## ADR-001 Canonical PostgreSQL

PostgreSQL만 authoritative state를 소유한다. vector, graph, RDF는 파생 projection이며 삭제 후 재생성 가능해야 한다.

## ADR-002 Evidence/Assertion separation

원문과 해석을 서로 다른 entity로 저장한다. assertion은 evidence locator 없이는 승인할 수 없다.

## ADR-003 Transactional outbox

canonical write, domain event, outbox message를 하나의 transaction으로 기록한다. broker는 후속 최적화다.

## ADR-004 Deterministic decisions

PASS/FAIL/REVIEW는 제한된 DSL, canonical measurement, effective rule version으로만 계산한다.

## ADR-005 Approval gate

report, BCF, CAD marker, 도면 revision 등 외부 영향 action은 proposal→approval→execution을 따른다.
