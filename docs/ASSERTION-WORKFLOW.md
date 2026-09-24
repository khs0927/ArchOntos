# Assertion Workflow

## Goal

An assertion is an interpretation of evidence, not evidence itself. It must never become an executable rule merely because an LLM produced it with high confidence.

## State machine

```text
UNREVIEWED
  ├─ APPROVED
  ├─ REJECTED
  └─ CONTESTED

CONTESTED
  ├─ APPROVED
  └─ REJECTED

APPROVED
  └─ CONTESTED

REJECTED
  └─ CONTESTED
```

Only `APPROVED` is promotable to the rule compiler.

## Provenance invariant

`assertion.source_version_id` and `assertion.evidence_span_id` must resolve to the same evidence/source-version pair. Migration 005 adds a composite foreign key so this is enforced by PostgreSQL, not merely application code.

## Idempotency

Candidates have a deterministic `assertion_key` derived from:

- evidence_span_id
- normalized natural-language assertion
- canonical structured payload

Re-running the same candidate does not create duplicates.

## Audit

Every review writes an immutable `assertion_review` row plus a domain event/outbox message. Approval publishes to `rules.assertion-approved`; other review decisions remain in the review topic.
