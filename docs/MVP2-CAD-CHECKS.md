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
- Widths are **effective widths** where the geometry allows it (section 1.2). The bbox
  approximation is kept only as a fallback and is flagged `approximate: true`.
- If a kind has no objects, its `min_*` and `*_total` facts are absent, not `0`, so a rule on
  them gives `REVIEW`. Counts are always present: `drawing.stair_count = 0` is a definite fact.

### 1.1 Object pairs (`archontos.cad.pairing`)

Rules that need two kinds at once do not read the second kind directly. They read **pair
facts** that an explicit, deterministic pairing step builds before any rule runs. Each pair of
an object has a status: `paired`, `unpaired` (no partner, or the inputs to look for one are
missing) or `ambiguous` (several partners fit equally well). Only `paired` pairs produce facts.

| Fact path | Unit | Pairing |
|---|---|---|
| `door.host_wall.<prop>`, `window.host_wall.<prop>` (`thickness`, `length`, `height`, ...) | m | IFC: `IfcRelFillsElement` -> opening -> `IfcRelVoidsElement` -> wall (stored as the door's `host_ref`, method `ifc:IfcRelFillsElement`). Otherwise geometric: wall lines within 0.35 m of the insert point; two parallel wall lines up to 0.6 m apart that overlap by 0.1 m are the faces of one wall and count as one host. A second host within 0.05 m of the best is a tie (`ambiguous`). |
| `stair.storey.elevation` | m | IFC `IfcRelContainedInSpatialStructure` storey of the stair (`storey` property), matched by name to the revision's model storeys. DXF has no storeys: `unpaired`. |
| `stair.storey.floor_number`, `stair.storey.basement_level`, `stair.storey.stair_count` | count | same; floors count 1, 2, ... above ground (`basement_level` 0), basements 1, 2, ... below (`floor_number` 0); `stair_count` is the number of stairs on that storey |
| `space.door_count`, `corridor.door_count` | count | doors whose insert point is in the space polygon or within 0.35 m of its boundary (a door between two rooms counts for both). `ambiguous` when any door has no plan location; `unpaired` when the space has no polygon. |

- **Fail closed.** A rule that reads a pair path of an object whose pair is `unpaired` or
  `ambiguous` gives `REVIEW` with reason `Pairing <status> (<pair>): <why>` and the pairing in
  `details.pairing`. It is never evaluated, so it can never `PASS` or `FAIL`.
- Rules that do not read a pair path ignore pair status.
- Per-object details list the pairs a rule read (`pairs.<name>.status/method/reason/
  related_object_ids`), so a reviewer can see which wall or storey was used.
- A rule that reads two kinds *directly* (`door.width` and `wall.length`) still gives `REVIEW`,
  now with the pair paths to use instead (`details.pair_paths`).
- `extract_fact_space(objects, model_meta=...)` takes the revision's `model_meta_json` for the
  storeys. The check endpoint and diff re-runs pass it.

### 1.2 Effective widths

| Fact | Method (`fact_meta.method`) | `approximate` |
|---|---|---|
| `space.width`, `corridor.width`, `stair.width` | `opposite_faces`: minimum perpendicular distance between antiparallel polygon edges that overlap and face each other across the inside (rotated rectangles give the short side, an L-shaped corridor its narrower arm) | false |
| `door.width`, `window.width` (DXF) | `attribute:<TAG>`: block attribute `CLEAR_WIDTH`, `유효폭`, `OPENING_WIDTH`, `LEAF_WIDTH`, `DOOR_WIDTH`, `WIDTH`, `문폭`, `폭` or `W` (first found; a bare number is in drawing units, `900`, `0.9m`, `90cm` accepted) | false |
| `door.width`, `window.width` (IFC / SketchUp) | `ifc_quantity` / `skp_quantity` (IFC `OverallWidth`, then Qto) | false |
| `wall.thickness` | `property` (IFC Qto), else `parallel_faces` (distance between the two face lines of a DXF wall) | false |
| any of the above when the better method is not possible | `bbox` / `block_bbox` (block extent, polygon bounding box) | **true** |

- The method is kept with the fact: per-object result details carry `fact_meta` for the facts a
  rule read, and the fact space carries `fact_meta` for `drawing.*_min_width` (from the object
  with the minimum). DXF objects record `width_source` as a property (parser `dxf-1.1`); DXF
  revisions parsed before that have no `width_source` and are treated as `bbox`.
- Polygons with no opposite parallel faces (triangles, coarse curves) fall back to the bbox.

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

- A per-object rule reads one object kind plus its pair facts (section 1.1). Pairing
  tolerances are fixed defaults (`PairTolerances`), not request parameters yet.
- Geometric host pairing needs DXF wall lines near the door insert point; IFC walls have no plan
  geometry here, so IFC doors pair only through `IfcRelFillsElement`.
- Corridors are found by room name. Clear widths ignore furniture, columns inside the polygon and
  door swing; curved outlines are only as good as their vertices.
- There are no storey or building facts (`building.floor_count`, `building.use_group`) yet.
  Callers pass them in `facts`. Without them, rules scoped on those facts give `REVIEW`.
