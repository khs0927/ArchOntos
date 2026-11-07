# MVP-2: regulation-to-CAD checks

MVP-2 evaluates approved, executable rules against one parsed DXF revision (MVP-1) and stores
one append-only verdict per rule: `PASS`, `FAIL` or `REVIEW`. Each verdict records the rule
version, its jurisdiction and effective interval, the legal evidence locator and the offending
CAD objects. Everything is file/JSON based. Land-parcel facts come in the request body, and
there are no network calls.

Code: `archontos.cad.checks` (pure), `archontos.cad.check_persistence` (PostgreSQL),
`apps/ingestion.py` (API), migration `015_cad_checks.sql`.

## 1. Fact extraction (`extract_fact_space`)

Input: the `arch_object` rows of a revision. Output: the rule DSL fact space.

| Fact path | Unit | Provenance (source objects) |
|---|---|---|
| `door.width`, `window.width`, `stair.width`, `column.width`, `stair.area`, `space.area`, `space.width`, `space.length`, `wall.length`, ... | m / m2 / deg | the object itself (evaluated per object) |
| `corridor.width`, `corridor.area` | m / m2 | spaces named like `복도`, `통로`, `corridor` or `hallway` |
| `drawing.<kind>_count` (wall, door, window, space, stair, column, opening, corridor) | count | all objects of that kind |
| `drawing.space_area_total`, `drawing.wall_length_total` | m2 / m | the contributing objects |
| `drawing.space_min_area`, `drawing.{door,window,stair,corridor}_min_width` | m2 / m | the object with the minimum value |
| `land_parcel.zone`, `land_parcel.districts`, `land_parcel.pnu`, `land_parcel.site_area` | - / m2 | request |
| `context.jurisdiction`, `context.reference_date` | - | request |

- Every numeric fact has a `fact_units` entry. Every fact path records its source `arch_object`
  ids and DXF entity handles.
- **Unit normalization** happens only here. Lengths in `mm`, `cm` or `m` become `m`. Areas in
  `mm2`, `cm2` or `m2` become `m2`. Other known units pass through unchanged. Unknown units,
  booleans and non-finite values are dropped, so the fact is missing.
- A space's `width` and `length` are the short and long sides of its bounding box. This only
  approximates rectangular rooms and corridors.
- If a kind has no objects, its `min_*` and `*_total` facts are absent, not `0`, so a rule on
  them gives `REVIEW`. Counts are always present: `drawing.stair_count = 0` is a definite fact.

## 2. Binding (`select_rules`)

A rule version runs only if all of these hold:

- `rule_version.status = 'active'`.
- It has at least one `basis` assertion, and every basis assertion is `approved`.
- `valid_from <= reference_date <= valid_to`, with an open end when `valid_to` is null.
- The rule's jurisdiction (the `source_document.jurisdiction_code` of its source) covers the
  run's jurisdiction. `KR` covers `KR-11`. `KR-11` does not cover `KR`.

Jurisdiction RLS also applies to the rules' source documents. `reference_date` defaults to
today (UTC). `rule_version_ids` can narrow the set. Skipped rules are returned in
`skipped_rules` with a reason.

## 3. Evaluation (`run_checks`)

- A rule that reads `<kind>.<property>` (for example `door.width`) runs **once per object** of
  that kind. The worst outcome wins (FAIL > REVIEW > PASS). The offending objects are the ones
  with that worst outcome. `details.objects` lists every object's verdict.
- A rule over `drawing.*`, `land_parcel.*`, `context.*` or caller facts runs once. On FAIL or
  REVIEW, the offending objects are the sources of the facts the rule reads.
- **Fail-closed:** each of these gives `REVIEW`, never `PASS`:
  - a missing fact;
  - a unit mismatch, where the rule's `fact_units` differ from the drawing's (for example a rule
    written in `mm` against facts in `m`);
  - a kind with no objects;
  - a rule that reads two object kinds;
  - a rule the engine cannot execute.
- Rules whose scope does not match are counted in `not_applicable_count` and are not stored as
  results.
- Caller `facts` cannot set `drawing`, `context`, `fact_units` or an object kind, because those
  come from the drawing and the run. Every `fact_units` entry must name a fact the caller
  supplied. Breaking either rule returns 422.

### Land parcel

The shape is compatible with site-pipeline `parcel_facts` and with the Ontology platform's
`POST /api/v1/regulation/evaluate {rule, land_parcel, facts}`:

```json
{"pnu": "1111010100100010000", "zone": "제2종일반주거지역",
 "districts": ["고도지구"], "site_area_m2": 250.5}
```

The site area can be given as `site_area: {"value", "unit"}`, `site_area_m2` or `area_m2`. It is
normalized to `land_parcel.site_area` in m2 and must be positive.

## Schema (migration 015)

| Table | Identity | Notes |
|---|---|---|
| `cad_check_run` | uuid5(revision, jurisdiction, date, inputs digest, checker version) | `jurisdiction_code` (RLS), `reference_date`, `land_parcel_json`, `facts_json`, `fact_units_json`, summary counts |
| `cad_check_result` | uuid5(run, rule_version) | `rule_version_id`, `outcome`, `reason`, `details_json`, the rule's `jurisdiction_code` / `valid_from` / `valid_to` / `binding`, `evidence_locator_json`, `fact_paths`, `arch_object_ids uuid[]`, `entity_handles text[]` |

- The existing `evaluation` and `decision` tables key on `object_version`. They cannot hold CAD
  object ids, entity handles or a run, so CAD checks use their own tables.
- Both tables are append-only. The `cad_reject_mutation` trigger applies to every role, and
  `archontos_app` has only SELECT and INSERT. `cad_check_run` has the same fail-closed
  jurisdiction policy as `cad_artifact`, and results are read through it.
- The inputs digest covers the land parcel, the caller facts and the sha256 of each bound rule's
  logic. Repeating a run with the same inputs and rules returns the existing run (HTTP 200,
  `created: false`). A new input or new rule logic creates a new run.
- Each new run writes a `domain_event` `CadChecksCompleted`, an `outbox_message` on topic
  `cad.checks-completed` and an `audit_log` row `cad.checks.run`.

## API (ingestion service)

| Method | Path | Permission |
|---|---|---|
| POST | `/v1/cad/revisions/{id}/checks`, body `{jurisdiction_code, reference_date?, land_parcel?, facts?, fact_units?, rule_version_ids?}` | `cad.check` (curator, reviewer, admin) |
| GET | `/v1/cad/revisions/{id}/checks` (runs) | `cad.read` |
| GET | `/v1/cad/revisions/{id}/checks/results?run_id=&outcome=&rule_version_id=&arch_object_id=&limit=&cursor=` | `cad.read` |

Each result carries:

- `outcome`, `reason` and `details`
- `rule_version_id`, `rule_title` and `binding`
- `jurisdiction_code`, `effective: {valid_from, valid_to}` and `reference_date`
- `evidence_locator`: a list of `{evidence_span_id, source_version_id, locator}`
- `fact_paths`, `arch_object_ids` and `entity_handles`

Cursors are bound to their filters.

| Status | When |
|---|---|
| 400 | a cursor is reused with different filters |
| 403 | the run's jurisdiction is not allowed for the caller |
| 404 | the revision is outside the caller's jurisdictions |
| 422 | the land parcel or the facts are invalid |

## Tests

- `tests/test_cad_checks.py` holds unit golden tests. The DXF fixtures are built in code
  (`tests/cad_fixtures.py`) and the in-memory rules go through the real compiler. Cases:
  - PASS: door ≥ 0.85 m.
  - FAIL: reports the offending door and its handles.
  - REVIEW for missing facts: no stairs, no land parcel.
  - REVIEW for a unit mismatch: a rule written in mm.
  - corridor minimum width, land parcel, binding filters and an unexecutable rule.
- `tests/integration/test_cad_checks_postgres.py` runs the full flow in CI: law, evidence,
  approved assertion, active rule, DXF upload, check run, results. It covers idempotency,
  filters, pagination, append-only enforcement, the effective date, 422 and RLS.

## Known limits

- A per-object rule reads one object kind. Rules that pair objects (a door and its wall, a stair
  and its storey) are not supported and give `REVIEW`.
- Corridors are found by room name. Widths come from bounding boxes, not true clear widths
  (door leaf versus clear opening, non-rectangular corridors).
- There are no storey or building facts (`building.floor_count`, `building.use_group`) yet.
  Callers pass them in `facts`. Without them, rules scoped on those facts give `REVIEW`.
