# MVP-5: issues, BCF 3.0, compliance reports and marker actions

## Issues (`src/archontos/cad/issues.py`, `issue_persistence.py`, migration `019_cad_issues.sql`)

`POST /v1/cad/checks/{run_id}/issues` (permission `cad.issue`) derives one issue per
`(rule_version, offending arch_object)` from the FAIL/REVIEW results of an MVP-2 check run
(a result without objects gives one drawing-level issue). Ids are uuid5 of
`(run, rule_version, object)`, so deriving again is a no-op.

* `origin = regression` (+ `diff_id`) when an MVP-3 re-run on this revision shows a regression
  (`PASS->FAIL` etc.) for the rule; otherwise `check`.
* Status is derived, never carried over: `open` (no superseding lineage diff with changes),
  `resolved` (a superseding diff re-ran this run and the rule PASSes on the target), `stale`
  (superseded without such a re-run, or still failing there; the target run has its own issues).
  Every new status is a new append-only `cad_issue_status` row; the latest is current.
* Each issue keeps the evidence locator(s), jurisdiction, effective interval, reference date,
  outcome, DXF handles, source refs (`ifc_guid`, `skp_persistent_id`) and the plan bbox (m).
* Outbox `cad.issues-derived` + audit `cad.issues.derive`, only when something new was written.

`GET /v1/cad/issues` (filters `run_id`, `revision_id`, `status`, `outcome`, `rule_version_id`,
`origin`; cursor pagination) reads through the jurisdiction RLS of `cad_issue`.

Tables (all append-only, `archontos_app` SELECT/INSERT only): `cad_issue` (RLS),
`cad_issue_status`, `cad_issue_note`, `cad_issue_artifact` (RLS; generated bcf/report/markers
outputs linked to run, revision, action and issues).

## BCF 3.0 (`src/archontos/cad/bcf.py`)

`GET /v1/cad/issues/export.bcf?run_id=|revision_id=[&status=]` returns a zip with `bcf.version`
(3.0), `project.bcfp`, `extensions.xml` and per topic `markup.bcf` + `viewpoint.bcfv`. Viewpoints
select components by IFC GlobalId (`ifc_guid` source ref); otherwise an orthogonal top camera
frames the object bbox (metres). DXF handles / SketchUp ids go to `Labels` and `ReferenceLinks`
(`archontos://...`). The zip is byte-deterministic. The export writes nothing.

## Compliance reports (`src/archontos/cad/report.py`, glue in `outputs.py`)

`POST /v1/cad/reports {run_id, diff_id?, title?, generated_at?}` (`cad.report`) renders the run
as canonical JSON and self-contained HTML, stores both as new artifacts, links them in
`cad_issue_artifact` (kind `report`, id = uuid5 of run + content sha) and emits
`cad.report-generated`. The same content returns 200 with the existing report.
`GET /v1/cad/reports/{id}?format=meta|json|html`.

## Marker actions (`src/archontos/cad/markers.py`)

1. `POST /v1/cad/checks/{run_id}/markers` (`action.propose`) builds a marker plan for the run's
   `open` issues with geometry (bbox converted to drawing units) and stores a
   `CadMarkersExport` action (`requires_approval`), including the power-cad payload preview.
2. `POST /v1/actions/{id}/approve` on the action service: four-eyes (proposer cannot approve).
3. `POST /v1/cad/markers/{id}/execute` (`action.execute`) only after approval: reads the original
   DXF, writes a NEW DXF artifact with the `ARCHONTOS-ISSUE` layer, links it (kind `markers`),
   emits `cad.markers-exported`, then marks the action succeeded. The original artifact is never
   written. The generic `POST /v1/actions/{id}/execute` refuses marker actions (409).

## Limits

* `to_power_cad_payload` key names have not been checked against the live power-cad
  `cad_create_many` schema; adapt before sending it to AutoCAD.
* Execute is not one transaction with the action status: the DXF output is written and linked
  first (idempotent per action), then the action is marked succeeded.
* Markers are DXF-only; IFC/SketchUp revisions get BCF instead.
* `resolved` needs a diff re-run (`rerun_checks`); a per-object fix while the rule still fails
  elsewhere stays `stale`.
* Issue notes have a table but no API yet.
