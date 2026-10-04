# MVP-0 — Korean Architecture Regulation Fabric

## Objective

Build a provenance-complete regulation fabric before CAD automation. The MVP succeeds only when answers can trace back to effective rule versions and source evidence.

## Priority-A ingestion

1. 국가법령정보센터 official structured source / official documents
2. 건축HUB and public-data APIs
3. official municipal ordinances
4. other government/public standards

General crawling is a gap-filler, not the primary source.

## Pipeline

```text
fetch official source
 -> persist raw artifact + hash
 -> source_document / source_version
 -> evidence_span
 -> assertion candidate
 -> human review
 -> approved assertion compiler
 -> rule / rule_version + rule_assertion links
 -> applicability
 -> canonical query APIs
 -> projection jobs
```

## Quality gates

- no assertion without evidence
- no decision without explicit rule_version
- unknown/ambiguous legal interpretation routes to REVIEW
- official authority metadata is categorical, not confidence-based
- extraction confidence and interpretation confidence are stored separately
- every effective-date comparison uses a concrete date

## Five required query classes

| Intent | Required canonical path |
|---|---|
| applicability | rule_version -> applicability -> object facts |
| jurisdiction comparison | jurisdiction -> applicability -> rule_version |
| temporal comparison | source/rule versions -> valid interval |
| authority classification | rule_version.authority_class + issuer/source |
| source evidence | rule -> rule_assertion -> assertion -> evidence_span -> artifact/source |

## 국가법령정보센터 adapter status

The first official-source adapter is implemented as a read-only DRF client. The approved OC value is supplied outside Git and removed from persisted/returned request metadata.

Implemented API paths:

- current-law list: `lawSearch.do?target=law`
- effective-version list/history: `lawSearch.do?target=eflaw`
- current/promulgation body: `lawService.do?target=law`
- effective-date body: `lawService.do?target=eflaw&MST=...&efYd=YYYYMMDD`

Raw JSON is retained in a `RawSourceEnvelope` with deterministic SHA-256 before normalization. Body parsing preserves article units, addenda and attachments, including source-provided attachment links.

## Current implementation status

Implemented through the canonical MVP-0 read path:

- immutable official-source artifact and `source_version` persistence
- effective interval / supersession maintenance for source versions
- deterministic article/paragraph/subparagraph/item/addendum/attachment evidence normalization
- stable `evidence_key` and normalized text hash
- assertion candidate idempotency and review history
- PostgreSQL provenance guard tying assertions to their evidence/source version
- approved-only safe rule compilation
- rule lifecycle: approved -> active, contested/rejected -> suspended
- compiled applicability persistence
- evaluation and decision persistence bound to an explicit `rule_version`
- canonical query executors for source evidence, authority, applicability, temporal comparison and jurisdiction comparison

Not yet implemented, and not covered by the golden path:

- projection workers; the outbox has no drainer outside normalization and
  `src/archontos/projection/base.py` still raises `NotImplementedError`
- `action` / `action_run` persistence and enforcement of the approval gate
- API authentication and identity propagation (P3 in `docs/ROADMAP.md`)

## Verification status

The release ticket below was executed against a real PostgreSQL instance
(`pgvector/pgvector:0.8.6-pg18`, the same image CI uses):

- migrations 001-007 applied in order into a throwaway schema
- the Golden Scenario end to end: ingest -> artifact -> source_version -> outbox
  -> normalization -> evidence -> assertion -> review -> compile -> evaluate
  -> decision -> query -> provenance traversal
- step 10, idempotent reprocess of an identical fixture, asserted separately in
  `tests/integration/test_mvp0_idempotency.py`: no new rows, no new outbox
  work, unchanged evidence identity, cleared conflict flag
- the same-MST/different-bytes case asserted against the real contract, which
  is a recorded `conflict=True` plus a high-severity `quality_flag`, not an
  exception

Result: `pytest` passes 85 tests with the database configured, and degrades to
80 passed / 5 skipped without one. `ruff check .` and `ruff format --check` are
clean. This is local evidence; hosted CI has not run successfully yet because
the GitHub Actions billing block is unresolved.

## Next implementation ticket

Provision a drainer for the outbox topics that normalization does not own, and
persist `action` / `action_run` so that `requires_approval` is enforced rather
than merely returned. Both are prerequisites for the P3 release gate in
`ops/RELEASE-CHECKLIST.md`, which is still entirely unchecked.
