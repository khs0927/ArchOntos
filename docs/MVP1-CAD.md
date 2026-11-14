# MVP-1: DXF/CAD object normalization

MVP-1 turns an uploaded DXF drawing into three layers of immutable data:

1. **Raw entities** (`cad_entity`): modelspace LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, INSERT,
   HATCH, TEXT and MTEXT, with handle, layer, type, block name, geometry JSON and a bbox.
2. **Architectural objects** (`arch_object`): wall, door, window, space, stair, column and
   opening. Each object has a kind, the mapping rule that produced it, properties (numbers
   always carry a unit, e.g. `{"area": {"value": 24.0, "unit": "m2"}}`), `fact_units`
   (`{"space.area": "m2"}`), geometry in metres, and the handles of its source entities.
3. **Provenance** (`arch_object_source`): which raw entities each object came from (`geometry`,
   or `label` for a room-name text).

The parser is pure and file-based. It makes no network calls. It needs the optional `cad`
extra (`pip install -e '.[cad]'`, ezdxf, MIT license). The Docker image and CI install it.

## Schema (migration 014)

| Table | Identity | Notes |
|---|---|---|
| `cad_artifact` | uuid5(source_id, sha256) | links the content-addressed `artifact` row; `project_id`, `source_key`, `source_id` (64-hex), `sha256`, `jurisdiction_code` (RLS), `created_by` |
| `cad_revision` | uuid5(cad_artifact, parser_revision) | `revision_label`, `parser_revision` and its sha256, the rule set JSON, units, counts, `content_digest` |
| `cad_entity` | uuid5(revision, bytes, handle) | `UNIQUE(revision_id, handle)` |
| `arch_object` | uuid5(revision, parser object id) | `kind` CHECK, `properties_json`, `fact_units_json`, `subject_ref_json` |
| `arch_object_source` | (object, entity) | provenance |

- **Append-only:** a trigger rejects UPDATE and DELETE on all five tables for every role.
  `archontos_app` also has only SELECT/INSERT on them.
- **Jurisdiction RLS:** `cad_artifact` uses the same fail-closed policy as `source_document`
  (migration 013). Every read joins through `cad_artifact`, so a caller sees only revisions in
  jurisdictions it is allowed. An upload to a jurisdiction the caller is not allowed returns 403.
- **Writes:** every new revision writes a `domain_event` (`CadRevisionIngested`), an
  `outbox_message` on topic `cad.revision-ingested` and an `audit_log` row (`cad.revision.ingest`).

## Determinism and idempotency

`parser_revision = "dxf-1.0+rules-<rule set version>"`. Uploading the same bytes for the same
`project_id`/`source_key` with the same parser revision returns the existing revision with
HTTP 200 and `created: false`. Nothing new is written, and the first `revision_label` is kept.
New bytes or a new rule-set version create a new revision (HTTP 201). Entity hashes, object
hashes and the revision `content_digest` are sha256 over canonical JSON. Coordinates are rounded
to 6 decimals. Entities are sorted by handle.

## Units

Units come from `$INSUNITS`: mm, cm, m, in or ft. If the code is 0 (unitless), the parser uses
`assume_units` (default `mm`, the usual unit for Korean drawings) and records `units_assumed`
on the revision. Any other code is rejected with 422. Lengths are reported in `m`, areas in `m2`
and rotation in `deg`, all from the unit set in `archontos.rules.units`.

## Mapping rules (`archontos.cad.rules`, default version `kr-aia-2026.11`)

Rules are checked in order and the first match wins. Each rule matches on entity type, a
layer-name regex and/or a block-name regex.

| Rule | Kind | Match |
|---|---|---|
| door-block / window-block | door / window | INSERT whose block name starts with `door`, `DR1`, `문` or `도어` / `window`, `WIN1` or `창` (not `창고`) |
| door-layer / window-layer / opening-layer | door / window / opening | INSERT on `A-DOOR`, `DOOR`, `문` (not `문자`) or `도어` / `A-GLAZ`, `A-WIND`, `WINDOW` or `창` / `A-OPEN` or `개구부` |
| column | column | INSERT or closed polyline/hatch on `A-COLS`, `S-COLS`, `COLUMN` or `기둥` |
| stair | stair | INSERT or closed polyline/hatch on `A-FLOR-STRS`, `A-STRS`, `STAIR` or `계단` |
| space | space | **closed** polyline or hatch on `A-AREA`, `A-ROOM`, `A-SPACE`, `ROOM`, `SPACE`, `실`, `공간` or `면적`; the name comes from TEXT/MTEXT inside the polygon on a label layer (`A-AREA-IDEN`, `ROOM-NAME`, `실명`, `실이름`) or on the space's own layer |
| wall | wall | LINE or polyline on `A-WALL` (not `-PATT`), `WALL` or `벽` |

Custom rule sets go through `rule_set_from_dict` and are validated: kinds, regexes, unique ids
and a version string. The version is part of `parser_revision`, so changing a rule produces a
new revision. Existing objects are never rewritten.

## Title-block facts (`archontos.cad.building_info`, default version `kr-titleblock-2026.11`)

`parse_dxf(..., building_rules=DEFAULT_BUILDING_RULES)` also reads the title block (도곽):

| Fact | INSERT attribute tag (any layer) | TEXT/MTEXT on `A-ANNO-TTLB`, `A-TTLB`, `TITLE`, `TTLB`, `TB`, `도곽`, `표제` |
|---|---|---|
| `building.use_group` | `USE`, `BLDG_USE`, `USE_GROUP`, `MAIN_USE`, `용도`, `주용도`, `건축물용도` | `주용도: 업무시설` |
| `building.floor_count` (count) | `FLOORS`, `FLOOR_COUNT`, `STOREYS`, `층수`, `지상층수`, ... | `지상 12층` |
| `building.basement_count` (count) | `BASEMENTS`, `지하층수`, ... (`없음` -> 0) | `지하 2층` |
| `sheet.storey_label` | `LEVEL`, `STOREY_NAME`, `FLOOR_NAME`, `해당층`, `층명` | `3층 평면도` -> `3층` |

The result is stored as `model_meta_json.building_info` (`rule_set_version`, `values`,
`sources` = handles / `handle:TAG`, `conflicts`) only when something was found, so drawings
without a title block keep their content digest. **Fail-closed**: no source -> no fact; two
sources with different values -> no fact plus a `conflicts` entry. A missing fact makes rules
that need it REVIEW. The facts appear in `GET .../facts` (provenance coverage `title_block`) and
in check runs; a caller that also sends `facts.building` / `facts.sheet` for such a revision
gets 422. Custom rules: `building_rule_set_from_dict` (each text pattern needs a named group `v`).

## Ontology contract

Each object stores an `archontos-aec-subject-ref/1` (validated by `AecSubjectRef`):

- `project_id`
- `object_id`: the `arch_object` id
- `source_id`: sha256 of `project_id` + `source_key`
- `source_byte_revision_id`: the file sha256
- `parser_revision_id`: sha256 of `parser_revision`

The three revision ids are always supplied together. `GET .../facts` returns the revision as
`aec-facts-export/1` with drawing totals (`drawing.space_area_total` in m2,
`drawing.wall_length_total` in m, and counts per kind) plus `fact_units`. The result loads
through `archontos.integration.ontology.load_facts_export`.

## API (ingestion service)

| Method | Path | Permission |
|---|---|---|
| POST | `/v1/cad/artifacts?project_id=&source_key=[&jurisdiction_code=KR&revision_label=&file_name=&assume_units=mm]`, raw DXF bytes as the body (`application/octet-stream`, max 50 MiB) | `cad.ingest` (curator, admin) |
| GET | `/v1/cad/revisions?project_id=&source_key=&sha256=&limit=&cursor=` | `cad.read` (viewer and every role) |
| GET | `/v1/cad/revisions/{id}` | `cad.read` |
| GET | `/v1/cad/revisions/{id}/objects?kind=&layer=&limit=&cursor=` | `cad.read` |
| GET | `/v1/cad/revisions/{id}/entities?layer=&entity_type=&limit=&cursor=` | `cad.read` |
| GET | `/v1/cad/revisions/{id}/facts` | `cad.read` |

Cursors are opaque keyset cursors (last id) bound to the filters. A cursor used with different
filters returns 400. Status codes: 422 for an unreadable DXF or bad units, 409 for an identity
conflict, 403 for a jurisdiction the caller is not allowed, 501 if the `cad` extra is missing.
Raw bytes are stored content-addressed under `raw/cad/<sha[:2]>/<sha>.dxf` with the existing
Local or MinIO store.

## Known limits (next steps)

- Walls are one object per line or polyline. Parallel lines are not merged into a wall with a
  thickness.
- Doors and windows come only from INSERT blocks. Arcs and lines drawn on a door layer are
  counted as unmapped.
- Polyline bulges are treated as straight segments (`approximated` is set). Nested blocks are
  not exploded. Only modelspace is read, not paper space or XREFs.
- Matching a door or window to its host wall, and assigning objects to a storey, are not done yet.
- DWG input is not supported: convert it to DXF first, for example with ODA File Converter.
