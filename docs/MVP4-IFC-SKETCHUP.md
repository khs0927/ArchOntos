# MVP-4: IFC / SketchUp model mapping

MVP-4 lets `arch_object` rows come from IFC models and SketchUp exports, not only from DXF.
Both new readers produce the same `ParsedDrawing` as the DXF parser. MVP-1 persistence, MVP-2
checks and MVP-3 diffs therefore run on IFC and SketchUp revisions without changes.

```
IFC (.ifc, STEP)       --ifcopenshell--> ModelElement[] --+
SketchUp JSON export   --JSON Schema---> ModelElement[] --+--> ModelMappingRuleSet --> ParsedDrawing
DXF                    --ezdxf (MVP-1)--------------------------------------------> ParsedDrawing
```

Code: `archontos.cad.model` (pure: elements, rules, units, detection, building facts),
`archontos.cad.ifc` (IFC reader), `archontos.cad.skp` (SketchUp JSON reader and schema),
`archontos.cad.links` (source refs and cross-source links). None of them make network calls.

## API (choice: extend `POST /v1/cad/artifacts`)

The existing upload endpoint accepts all three formats. A separate `/v1/models` upload would
have duplicated identity, idempotency, RLS and revision listing. Only cross-source links get
their own path.

| Endpoint | Permission | Notes |
|---|---|---|
| `POST /v1/cad/artifacts?source_format=auto\|dxf\|ifc\|skp_json` | `cad.ingest` | `auto` (default) checks the content first: `ISO-10303-21` means IFC and a leading `{` means SketchUp JSON. It then checks the `file_name` extension (`.ifc`, `.json`) and the content type (`application/x-step`, `model/ifc`, `application/json`). Anything else is DXF. A binary `.skp` returns **415**. `assume_units` applies to DXF only. |
| `GET /v1/cad/revisions/{id}` | `cad.read` | Adds `source_format` and `model_meta` (IFC schema, storeys with `elevation_m`, length unit). For a model source, `dxf_version` holds the format version (`IFC4`, `archontos-skp-export/1`). |
| `GET /v1/cad/revisions/{id}/facts` | `cad.read` | Adds `building.floor_count`, `building.basement_count` and `building.storey_count` (provenance `coverage: model`) when the model has storeys, and `drawing.slab_count` when it has slabs. |
| `POST /v1/models/links` `{object_a_id, object_b_id, note?}` | `cad.link` (new; curator, admin) | Manual link. The two objects must be in different revisions of the same project and jurisdiction. Sending the same pair again (in either order) returns 200. |
| `GET /v1/models/links?project_id&revision_id&object_id&method&limit&cursor` | `cad.read` | Lists links with each side's kind, `source_format` and handles. Cursor paging. |

## Identity and mapping

| Source | `cad_entity.handle` / `arch_object.source_handles` | `arch_object_source_ref` | `layer` |
|---|---|---|---|
| DXF | DXF handle | `dxf_handle` (geometry handle) | DXF layer |
| IFC | `GlobalId` | `ifc_guid` | name of the containing `IfcBuildingStorey`, or `(no storey)` |
| SketchUp JSON | `persistent_id` | `skp_persistent_id` and `ifc_guid` when exported | tag (layer) |

`parser_revision` is `ifc-1.0+rules-<v>` or `skp-json-1.0+rules-<v>`, and ids are uuid5 values
as in MVP-1. Re-uploading the same bytes returns the same revision (200). Because IFC GlobalIds
and SketchUp persistent ids stay stable across exports, the MVP-3 diff pairs objects across
revisions by handle.

### Mapping rules (`DEFAULT_MODEL_RULE_SET`, version `model-2026.11`)

Rules are tried in order and the first match wins. Every pattern a rule declares must match.
`class_pattern` is tested against the IFC class (for SketchUp, the `ifc_type` attribute).
`tag_pattern` is tested against the tag or storey. `definition_pattern` is tested against the
component definition name, or the entity name when there is none.

| Rule | Kind | Formats | Match |
|---|---|---|---|
| ifc-door / ifc-window | door / window | ifc, skp_json | `IfcDoor`, `IfcWindow` (+`StandardCase`) |
| ifc-wall | wall | ifc, skp_json | `IfcWall`, `IfcWallStandardCase`, `IfcWallElementedCase` |
| ifc-space | space | ifc, skp_json | `IfcSpace` |
| ifc-stair | stair | ifc, skp_json | `IfcStair`, `IfcStairFlight`. An `IfcStair` that aggregates flights is skipped so it is not counted twice; the flights carry the facts. |
| ifc-column / ifc-slab | column / slab | ifc, skp_json | `IfcColumn`, `IfcSlab` (+`StandardCase`) |
| ifc-opening | opening | ifc, skp_json | `IfcOpeningElement` (SketchUp only; the IFC reader does not extract openings) |
| skp-door-def / skp-window-def | door / window | skp_json | definition `door…`, `DR1`, `문`, `도어` / `window…`, `WIN1`, `창` (not `창고`) |
| skp-*-tag | door, window, column, stair, slab, space, wall | skp_json | tags `A-DOOR`/`문`, `A-GLAZ`/`창`, `A-COLS`/`기둥`, `A-STRS`/`계단`, `S-SLAB`/`슬래브`, `A-ROOM`/`실`, `A-WALL`/`벽` (as in MVP-1) |

`model_rule_set_from_dict` validates custom rule sets: kind, regexes, unique ids, formats and
the version string. The `slab` kind is new; migration 018 adds it to the `arch_object` and
`cad_object_change` kind CHECKs. MVP-2 checks do not read `slab`.

### Quantities and units

Every number is stored as `{"value", "unit"}` in `m`, `m2`, `m3`, `deg` or `count`. It also
appears in `fact_units` (`door.width: m`).

- **IFC**: scales come from `IfcProject.UnitsInContext`. `IfcSIUnit` prefixes are raised to the
  dimension (MILLI METRE gives 0.001 for lengths). `IfcConversionBasedUnit` (feet, inches) uses
  its conversion factor. A missing area or volume unit falls back to length² or length³.
  Lookup order is attributes (`OverallWidth`/`OverallHeight`), then `Qto_*` sets, then IFC2x3
  `BaseQuantities`, then property sets. The first name found wins.

| Kind | Properties (IFC names, in priority order) |
|---|---|
| door / window | width ← `OverallWidth`, `Width`; height ← `OverallHeight`, `Height`; area ← `Area` |
| wall | length ← `Length`; height ← `Height`; thickness ← `Width`; area ← `NetSideArea`, `GrossSideArea`; volume |
| space | area ← **`NetFloorArea`**, `GrossFloorArea`; gross_area; perimeter ← `GrossPerimeter`; height ← `Height`, `FinishCeilingHeight`; volume; name ← `LongName`, `Name` |
| stair | width ← `Width`, `ClearWidth`, `NominalWidth`; length; height; riser_height; tread_length; riser_count ← `NumberOfRiser` |
| column | height ← `Length`, `Height`; width; depth; area ← `CrossSectionArea`; volume |
| slab | area ← `NetArea`, `GrossArea`; thickness ← `Width`, `Depth`; perimeter; volume |

  Geometry is the plan footprint of an `IfcExtrudedAreaSolid` body (rectangle or
  arbitrary closed profile, placed in world coordinates) for spaces and slabs. Other elements
  get the placement origin as a point.
- **SketchUp JSON**: every length is multiplied by the factor for `model.units`
  (`inch` 0.0254, `ft` 0.3048, `mm`, `cm`, `m`). Without explicit `quantities`, values come
  from the world bbox: width/length = longer plan side, depth/thickness = shorter side,
  height = z extent. A space or slab without a `footprint` uses the bbox plan area. Explicit
  `quantities` (with their own units) always win.

### Building facts

`building.floor_count` counts the storeys at or above -0.5 m (지상층).
`building.basement_count` counts the storeys below -0.5 m (지하층). `building.storey_count`
counts all storeys. These facts come from the IFC `IfcBuildingStorey` list or from SketchUp
`storeys`. MVP-2 checks get them automatically. A caller that also sends `facts.building`
for such a revision gets 422, because the model is the source.

## Cross-source links (migration 018)

- `arch_object_source_ref (arch_object_id, ref_kind, ref_value)` is written for every new
  object.
- `model_object_link` holds `shared_ref` links. These are created at ingest when an object
  in the new revision has the same `ifc_guid` as an object in another revision. That revision
  must be of a different `source_format`, with the same `project_id`, the same
  `revision_label` (the same design revision exported twice) and the same jurisdiction.
  `manual` links are created through the API.
- Link ids are uuid5 values over the ordered pair and the method. Rows are append-only (the
  `cad_reject_mutation` trigger, and `archontos_app` has SELECT/INSERT only). Links use the
  same fail-closed jurisdiction RLS as `cad_artifact`.
- DXF objects only have a `dxf_handle`, which is not shared. They are linked to IFC objects
  by geometry (below) or manually.

### Geometric links (P2, migration 022, `src/archontos/cad/geolink.py`)

When a DXF or IFC revision is ingested, it is matched against the IFC / DXF revisions with the
same `project_id`, jurisdiction and `revision_label`:

- Only the same `kind` is compared (`wall`, `door`, `window`, `space`, `stair`, `column`,
  `opening`; `slab` is IFC-only). Geometry is the stored `geometry_json`, which both parsers
  already normalise to metres (`$INSUNITS` / IFC length unit), so a mm drawing and a metre IFC
  compare directly.
- Score: plan bboxes grown by `tolerance/2` (default tolerance 0.3 m, so points get an area);
  `confidence = 0.6 * IoU + 0.4 * max(0, 1 - centroid distance / tolerance)`. A pair is kept
  when the centroid distance is within the tolerance or IoU >= 0.5, and confidence >= 0.5.
- Matching is one-to-one, greedy by confidence (ties by ids), deterministic.
- Rows: `method = 'geometric'`, `confidence` (0..1, required for geometric, NULL otherwise),
  `note = "kind=... iou=... d=...m"`. Objects that already have a `manual` or `shared_ref` link
  in either revision are never auto-linked, and existing links are never changed (append-only).
  `GET /v1/models/links?method=geometric` lists them; each link carries `confidence`.

### Openings (`IfcOpeningElement`, IFC parser `ifc-1.2`)

`IfcOpeningElement` (and `IfcOpeningStandardCase`) is now read as kind `opening` with
`width`/`height`/`depth` from `Qto_OpeningElementBaseQuantities`, `host_ref` (the voided
wall/slab, `IfcRelVoidsElement`) and `filler_refs` (doors/windows, `IfcRelFillsElement`).
Doors and windows gain `opening_ref` next to `host_ref`. The parser version bump gives IFC
revisions new parser revisions (existing objects are not rewritten).

## SketchUp export contract `archontos-skp-export/1`

`archontos.cad.skp.SKP_EXPORT_SCHEMA` (JSON Schema 2020-12) validates the export. The schema
rejects unknown keys. The first violation is reported as 422 with its JSON path.

```json
{
  "schema": "archontos-skp-export/1",
  "model": {"name": "주택 A", "guid": "…", "units": "mm", "exporter": "skp-exporter 0.1",
            "sketchup_version": "25.0"},
  "storeys": [{"name": "1F", "elevation": 0}, {"name": "B1", "elevation": -3000}],
  "entities": [
    {
      "persistent_id": 101,
      "entity_type": "ComponentInstance",
      "tag": "문",
      "name": "D1",
      "definition": "Door 900",
      "ifc_type": "IfcDoor",
      "ifc_guid": "2O2Fr$t4X7Zf8NOew3FLOH",
      "storey": "1F",
      "bbox": {"min": [500, 0, 0], "max": [1400, 50, 2100]},
      "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 500,0,0,1],
      "footprint": [[0,0],[900,0],[900,50],[0,50]],
      "quantities": {"width": {"value": 900, "unit": "mm"}},
      "attributes": {"any": "exporter data"}
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `model.units` | yes | Unit of every coordinate: `inch` (SketchUp internal), `ft`, `mm`, `cm` or `m`. |
| `entities[].persistent_id` | yes | `Entity#persistent_id` (integer, or a string of 1 to 64 `[0-9A-Za-z_$-]`). Must be unique. |
| `entity_type` | yes | `ComponentInstance` or `Group`. |
| `tag` | yes | Tag (layer) name; `Untagged` is allowed. |
| `bbox` | yes | World-space bounds `{min:[x,y,z], max:[x,y,z]}` in `model.units`. |
| `definition` | no | Component definition name. |
| `ifc_type` / `ifc_guid` | no | SketchUp IFC classification (`IfcDoor`) and its 22-character GlobalId. A shared `ifc_guid` creates the link to the IFC element. |
| `storey`, `name` | no | Storey label; display name (it becomes the space name). |
| `transform` | no | `Transformation#to_a` (16 numbers, column-major). Stored on the raw entity. |
| `footprint` | no | Closed plan polygon `[[x,y],…]` (at least 3 points) for spaces and slabs. |
| `quantities` | no | `{name: {value, unit}}` with unit `mm`…`ft3`, `deg`, `rad` or `count`. Normalised and preferred over values derived from the bbox. |
| `storeys[]` | no | `{name, elevation}` (elevation in `model.units`). Gives the `building.*` facts. |

### SketchUp exporter (`tools/sketchup/archontos_export.rb`)

A small, read-only Ruby script that writes this contract:

```ruby
load 'C:/path/to/ArchOntos/tools/sketchup/archontos_export.rb'
ArchOntos::SkpExport.export('C:/temp/model.archontos.json')
```

It runs in the SketchUp Ruby console or through a SketchUp MCP `execute_ruby` / `eval_ruby`
call. Lengths are written in `mm` (converted from SketchUp inches). Every visible Group /
ComponentInstance is exported with its world bbox and transform; nested ones are flattened,
except inside IFC-classified or door/window components. `ifc_type` / `ifc_guid` come from the
IFC classification (`AppliedSchemaTypes`, `IFC 4` / `IFC 2x3` → `GlobalId`). `storey` comes
from the entity attribute `archontos/storey`. `storeys` come from the model attribute
`archontos/storeys` (JSON list of `{name, elevation}` in mm). Groups with a horizontal face also
get a `footprint`. The script cannot run in CI: `tests/test_skp_exporter_contract.py` validates
`tools/sketchup/sample_export.json` (its output for the documented fixture room) against the
schema and checks that the Ruby emits only contract keys.

### Revit → IFC (recommended export setup; no Revit automation in this repo)

Export from Revit (File → Export → IFC, or the open-source *IFC Exporter for Revit*) with:

| Setting | Value | Why |
|---|---|---|
| IFC version | IFC4 Reference View (or IFC2x3 Coordination View 2.0) | both parsed; IFC4 preferred |
| Space boundaries | none | not used |
| Export base quantities | **on** | `Qto_*BaseQuantities` give widths, areas, heights |
| Split walls/columns by level | on | one storey per element |
| Export rooms / spaces | **on** (3D view or "Export rooms in 3D views") | `IfcSpace` with `LongName` = room name |
| Include IFC openings / "Export openings as separate entities" | **on** | `IfcOpeningElement` + `IfcRelVoidsElement` / `IfcRelFillsElement` give door/window hosts |
| Store the IFC GUID in an element parameter after export | **on** | keeps GlobalIds stable across exports (shared_ref links, BCF) |
| Coordinates | Shared coordinates / survey point | same origin as the DXF sheets so geometric links match |
| Project units | mm (any SI length unit is fine) | converted to metres on import |
| Levels | name them like the drawings (`B1`, `1F`, ...) | storeys -> `building.*` facts, stair pairing |

Use the same `revision_label` for the Revit IFC and the DXF sheets of one design revision so
`geometric` links are created. No pyRevit or Dynamo script is needed; if you automate the export
on the workstation, the Revit MCP servers (`rvt-mcp` `revit_export_ifc`, `horizun-revit`) can
call the same exporter. ArchOntos never calls them; the user's tools push files to
`POST /v1/cad/artifacts`.

## Verification

- Pure unit tests (`tests/test_cad_model.py`) cover units, IFC unit scales and quantity
  selection with fake entities, rules, schema validation, detection, building facts, the
  MVP-2 fact space and the MVP-3 diff on SketchUp revisions. One IFC test builds an IFC4
  model with ifcopenshell (`pytest.importorskip`).
- `tests/integration/test_model_postgres.py` runs against PostgreSQL: upload, idempotency,
  facts, a check run (PASS), a diff with a PASS→FAIL transition, 415 for binary `.skp`, IFC↔SKP
  `shared_ref` links, manual links, paging and append-only enforcement.
- CI installs `.[dev,cad,bim]`. ifcopenshell (LGPL-3.0) is in the optional `bim` extra.
  `jsonschema` (MIT) is a core dependency.
