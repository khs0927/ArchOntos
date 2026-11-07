# Roadmap

## P0: Normalization foundation
- 조문 tree parser
- evidence locator canonical format
- 부칙·별표 fixture
- idempotent persistence

## P1: Assertion and review
- assertion candidate API
- reviewer queue
- review status transition
- provenance validation

## P2: Rule and query
- approved assertion compiler
- applicability model
- temporal/jurisdiction/source query executors
- golden tests

## P3: MVP-0 release candidate
- E2E
- API auth/identity propagation — done: named API keys, per-request actor, role-based permissions,
  four-eyes approval, least-privilege DB role, audit_log append-only and jurisdiction RLS
  (ops/RUNBOOK.md); optional OIDC/JWT identity (`ARCHONTOS_OIDC_*`, RS256 via JWKS) - done
- migration/backup/restore runbook - done: `ops/backup.sh`, `ops/restore.sh`, CI round trip
  (`ops/backup-restore-check.sh`), rollback procedure in ops/RUNBOOK.md
- metrics and structured audit logs - done: Prometheus counter, JSON request log with actor and
  request id (`ARCHONTOS_LOG_FORMAT`)
- 0.1.0-rc1: tag once CI is green on main; open items are listed in README "Still incomplete"

## 후속
- pgvector — done: embeddings (fastembed/hashing), HNSW, paginated similarity search
- DXF
- IFC/BCF
- impact analysis
- approved actions — done: proposal → approval gate → execution with audit

> 일정은 고정 주차보다 Definition of Done 기준으로 진행한다.
