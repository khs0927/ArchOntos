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

## MVP-1: CAD object normalization (in progress)

- [x] DXF parser (ezdxf, `cad` extra), deterministic ids/hashes, idempotent persistence
- [x] Migration 014: cad_artifact / cad_revision / cad_entity / arch_object / provenance, append-only, jurisdiction RLS
- [x] Versioned mapping rules (Korean 벽/문/창/실/계단/기둥 + AIA A-WALL/A-DOOR/...), spaces with m2 area
- [x] `/v1/cad/*` API with permissions, cursor pagination, outbox events, `aec-facts-export/1` facts
- [ ] Wall pairing (thickness), door/window host walls, storeys, DWG conversion. See docs/MVP1-CAD.md

## MVP-2: regulation-to-CAD checks (in progress)

- [x] Fact extraction from arch_object (lengths in m, areas in m2; provenance by object id and handle)
- [x] Binding: active rules with approved basis, effective on the reference date, in the jurisdiction; land-parcel facts
- [x] Migration 015: cad_check_run / cad_check_result (append-only, RLS); outbox topic `cad.checks-completed`
- [x] `/v1/cad/revisions/{id}/checks` run and results API with filters and cursors
- [ ] Object pairing (door/wall, stair/storey), storey and building facts, clear-width geometry. See docs/MVP2-CAD-CHECKS.md

## MVP-3: revision diff (in progress)

- [x] Deterministic object matching (DXF handle, then centroid/IoU), added/removed/modified/unchanged with unit-carrying deltas and configurable tolerances
- [x] Impact on MVP-2 check results, stale marking of base results, optional re-run with outcome transitions
- [x] Migration 017: cad_revision_diff / cad_object_change (append-only, RLS); outbox topic `cad.revision-diffed`
- [x] `/v1/cad/diffs` API with `cad.diff`, filters and cursors
- [ ] Wall/opening pairing across revisions, storey-aware matching, Ontology stale-edge export. See docs/MVP3-REVISION-DIFF.md

## 후속
- pgvector — done: embeddings (fastembed/hashing), HNSW, paginated similarity search
- DXF
- IFC/BCF
- impact analysis
- approved actions — done: proposal → approval gate → execution with audit

> 일정은 고정 주차보다 Definition of Done 기준으로 진행한다.

## MVP-4: IFC / SketchUp mapping
- [x] Source-agnostic model import (`archontos.cad.model`); IFC via ifcopenshell (`bim` extra); SketchUp JSON export contract with JSON Schema
- [x] Versioned model mapping rules (`model-2026.11`), units normalized to m/m2/m3, `slab` kind
- [x] Migration 018: `source_format`, `model_meta_json`, `arch_object_source_ref`, `model_object_link` (append-only, RLS)
- [x] `building.floor_count` / `basement_count` from IFC storeys; MVP-2 checks and MVP-3 diffs on IFC/SKP revisions
- [x] `/v1/models/links` (auto `shared_ref` by IFC GlobalId, manual links) with `cad.link`
- [ ] SketchUp Ruby exporter (via the SketchUp MCP), Revit→IFC producer automation, geometry-based IFC↔DXF auto-linking, IfcOpeningElement/IfcRelVoids handling
