# ArchOntos

**ArchOntos** is the implementation repository for **OAIF (Open Architecture Intelligence Fabric)**: an evidence-backed architecture regulation and design intelligence framework.

The core invariant is simple:

> **PostgreSQL is canonical truth. Vector, graph, RDF, search and AI views are rebuildable projections.**

## Current milestone

This repository starts with **MVP-0: Korean Architecture Regulation Fabric** and establishes the contracts needed for later CAD, IFC/BCF and design-action workflows.

Implemented foundation:

- canonical PostgreSQL schema with provenance, versions, evidence, assertions and rules
- role-based hyperedges and immutable domain events
- transactional outbox for projection/event delivery
- JSON rule DSL evaluator with PASS / FAIL / REVIEW outcomes
- five-query MVP-0 intent router
- service boundaries for ingestion, normalization, rule engine, projection and action
- FastAPI health and contract endpoints
- PostgreSQL 18 + pgvector development stack
- Prometheus-ready metrics endpoint
- Helm/GitOps deployment skeleton
- unit tests for rule evaluation, data contracts and query routing

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
docker compose up -d postgres minio
```

Run the rule engine API:

```bash
uvicorn apps.rule_engine:app --reload --port 8003
```

Then open `/docs` or call:

```bash
curl http://localhost:8003/health
```

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
