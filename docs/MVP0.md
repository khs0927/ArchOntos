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
 -> assertion
 -> reviewed rule / rule_version
 -> applicability
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
| source evidence | rule -> evidence_span -> artifact/source |

## Not in the first commit

The repository intentionally does not pretend to have a live law.go.kr ETL until the official source/API contract and access method are bound and tested. The source adapter boundary exists, and the next implementation ticket is to add the real official ingestion path with fixtures and provenance tests.
