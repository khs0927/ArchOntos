# ADR-0001: PostgreSQL canonical truth with transactional outbox

**Status:** Accepted

## Context

OAIF needs vector, graph, RDF and GraphRAG representations, but allowing each store to own state creates version drift and weakens legal provenance.

## Decision

Canonical entities, versions, evidence, assertions, rules, evaluations, decisions, actions and events live in PostgreSQL. Projection stores are rebuildable. Integration events are persisted in the same transaction as canonical changes and delivered through an outbox worker.

## Consequences

- projection loss is recoverable
- broker outage cannot lose canonical writes
- operational complexity stays low for MVP-0
- later NATS/Kafka adoption does not require changing domain contracts
