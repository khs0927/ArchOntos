# OAIF v4.5 implementation architecture

## Decision: canonical state + rebuildable projections

PostgreSQL owns canonical state. pgvector, graph stores, RDF stores, GraphRAG indexes and caches are projections. They can lag, fail or be rebuilt without changing the legal/provenance truth.

### Why the first release is a modular monorepo

OAIF v4.5 describes five runtime service boundaries. They are preserved in code, container commands and Helm values, but initially share one repository and domain package. This avoids early distributed-systems tax while keeping future extraction straightforward.

The initial event transport is a **transactional outbox** stored with canonical changes. A future NATS/Kafka publisher can drain `outbox_message`; canonical writes do not depend on broker availability.

## Service boundaries

- ingestion — source adapters, raw artifact manifests, source-specific fetch contracts
- normalization — evidence/assertion contracts and quality flags
- rule-engine — deterministic rule execution and evaluation output
- projection — projection checkpoints and rebuild workers
- action — query routing and approval-gated action proposals

## Provenance chain

```text
decision
  -> evaluation
  -> rule_version
  -> rule/source_version
  -> assertion
  -> evidence_span
  -> artifact/source_version
  -> source_document
```

A generated answer must never collapse legal authority and model confidence into one score.

## Events and CQRS

`domain_event` records immutable facts. `outbox_message` provides reliable handoff to projection workers. A projection records `projection_checkpoint.last_sequence`, so a projection can be replayed from sequence 1.

This is deliberately lighter than full event sourcing: canonical tables remain the primary state model. Events are an audit/integration stream, not the only way to reconstruct canonical state.

## Security

MVP development uses application authorization boundaries. `db/migrations/002_rls_template.sql` documents the production RLS pattern but is not enabled until authenticated identity claims are propagated into PostgreSQL session settings.

ABAC dimensions reserved by the domain model:

- jurisdiction
- authority class
- binding
- review status
- user/service role

## Scale path

1. PostgreSQL 18 + pgvector only
2. transaction outbox + projection workers
3. optional Apache AGE or Neo4j projection when graph traversal justifies it
4. optional RDF/Jena projection for interoperability
5. optional NATS/Kafka when service isolation/throughput justifies broker operations
6. independent Kubernetes scaling per service boundary
