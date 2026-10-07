# ArchOntos

**ArchOntos** is the implementation repository for **OAIF (Open Architecture Intelligence Fabric)**: an evidence-backed architecture regulation and design intelligence framework.

The core invariant is simple:

> **PostgreSQL is canonical truth. Vector, graph, RDF, search and AI views are rebuildable projections.**

## Current milestone

This repository starts with **MVP-0: Korean Architecture Regulation Fabric** and establishes the contracts needed for later CAD, IFC/BCF and design-action workflows.

Implemented foundation:

The markers below are load-bearing. `(schema only)` means the tables exist in
`db/migrations` with no Python behind them yet, and `(shell)` means an endpoint
exists but does not do the work its name implies. Nothing listed here is
claimed to be more complete than it is.

Complete and exercised against a real PostgreSQL instance:

- canonical PostgreSQL schema with provenance, versions, evidence, assertions and rules
- immutable domain events
- transactional outbox: producers in the ingestion, assertion, rule-compilation
  and evaluation paths, with the normalization worker as the only consumer
  (`projection` draining is not built yet, see below)
- JSON rule DSL evaluator with PASS / FAIL / REVIEW outcomes, fail-closed on a
  missing fact or an unknown operator
- deterministic law.go.kr article/addendum/attachment evidence normalizer
- idempotent evidence identity and canonical evidence persistence
- assertion candidate + human review state machine
- approved-assertion-only safe rule compiler
- rule active/suspended lifecycle tied to assertion review state
- five-query MVP-0 intent router and canonical query executors
- evaluation and decision persistence bound to an explicit `rule_version`
- immutable artifact/source-version ingestion with effective interval maintenance
- official law.go.kr DRF client for current/effective versions, articles and attachments
- canonical source-evidence, authority, applicability, temporal and jurisdiction query APIs
- unit and integration tests, including a real-PostgreSQL golden path

Filled in this pass (PostgreSQL path is `ARCHONTOS_ACTION_BACKEND=postgres`; default `memory` keeps smoke tests DB-free):

- role-based hyperedges (`src/archontos/graph/hyperedges.py`, `POST /v1/hyperedges`)
- action / action_run persistence and the approval gate (`POST /v1/actions/{id}/approve|reject|execute`). A report cannot run from `proposed` while `requires_approval` is true
- projection worker (`src/archontos/projection/worker.py`) replays `domain_event` into `embedding_projection` and advances `projection_checkpoint` (`POST /v1/projections/drain`, `/rebuild`)
- `REQUEST_COUNT` is incremented per request
- optional API key gate: set `ARCHONTOS_API_KEYS`; `/health` and `/metrics` stay open

Still incomplete:

- embedding vectors are not computed (`embedding` stays null until an embedder is configured)
- Helm has a ServiceAccount and default-deny NetworkPolicy, not GitOps reconciliation or an HPA
- identity propagation beyond a shared API key (RLS, per-user actor) is still P3

## Architecture

```text
Official sources / CAD / BIM / project documents
                    |
                    v
            Ingestion Service
                    |
                    v
          Normalization Service
                    |
                    v
     +--------------------------------+
     | PostgreSQL canonical state     |
     | source/evidence/assertion/rule |
     | object/hyperedge/event/outbox  |
     +--------------------------------+
             |              |
             |              +--> Rule Engine --> evaluation/decision
             |
             +--> Projection workers --> pgvector / AGE / Neo4j / RDF
                                  |
                                  v
                              Action API
```

The initial repository is a **modular monorepo**. Each service has a separate executable boundary, but shares domain contracts. This minimizes early operational cost while preserving a clean path to independent Kubernetes deployment.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest
```

Run infrastructure:

```bash
docker compose up -d postgres
# optional S3-compatible artifact store (dev only, see docs/ARTIFACT-STORAGE.md):
# docker compose --profile s3 up -d minio   # and ARCHONTOS_ARTIFACT_BACKEND=minio
```

All published ports (Postgres, MinIO, apps 8001-8005) bind to `127.0.0.1`. Set `ARCHONTOS_API_KEYS` before exposing them; `/health` and `/metrics` stay open.

Run the ingestion API:

```bash
uvicorn apps.ingestion:app --reload --port 8001
```

Run the rule engine API:

```bash
uvicorn apps.rule_engine:app --reload --port 8003
```

The law.go.kr adapter uses the official DRF Open API. Configure its approved OC value outside Git:

```bash
export ARCHONTOS_LAWGO_OC='...'
curl 'http://localhost:8001/v1/sources/lawgo/search?query=건축법'
```

The OC value is intentionally excluded from canonical request metadata and error output.

## Five MVP-0 query intents

1. applicability — "이 규정이 어느 건축물에 적용돼?"
2. jurisdiction comparison — "서울과 부산 기준이 달라?"
3. temporal comparison — "2025년 허가 당시와 현재 기준이 달라?"
4. authority classification — "이 조항이 시행령인지 시행규칙인지?"
5. source evidence — "이 기준의 별표 원문을 보여줘."

## Important governance rules

- Evidence and interpretation are separate records.
- `authority_class` is not a probability.
- extraction and interpretation confidence never determine legal authority.
- every decision binds to an explicit `rule_version`.
- GraphRAG/search can explain and retrieve; it does not make the authoritative PASS/FAIL decision.
- write actions are proposals until an explicit approval gate is satisfied.
- projection stores can be deleted and rebuilt without loss of canonical truth.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/MVP0.md`](docs/MVP0.md).
