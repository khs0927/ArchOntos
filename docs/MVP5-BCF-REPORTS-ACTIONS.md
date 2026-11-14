# MVP-5: issues, BCF 3.0, compliance reports and marker actions

## Issues (`src/archontos/cad/issues.py`, `issue_persistence.py`, migration `019_cad_issues.sql`)

`POST /v1/cad/checks/{run_id}/issues` (permission `cad.issue`) derives one issue per
`(rule_version, offending arch_object)` from the FAIL/REVIEW results of an MVP-2 check run
(a result without objects gives one drawing-level issue). Ids are uuid5 of
`(run, rule_version, object)`, so deriving again is a no-op.

* `origin = regression` (+ `diff_id`) when an MVP-3 re-run on this revision shows a regression
  (`PASS->FAIL` etc.) for the rule; otherwise `check`.
* Status is derived, never carried over: `open` (no superseding lineage diff with changes),
  `resolved` (see below), `stale`
  (superseded without evidence that the issue is gone; the target run has its own issues).
  Every new status is a new append-only `cad_issue_status` row; the latest is current.
* Each issue keeps the evidence locator(s), jurisdiction, effective interval, reference date,
  outcome, DXF handles, source refs (`ifc_guid`, `skp_persistent_id`) and the plan bbox (m).
* Outbox `cad.issues-derived` + audit `cad.issues.derive`, only when something new was written.

`GET /v1/cad/issues` (filters `run_id`, `revision_id`, `status`, `outcome`, `rule_version_id`,
`origin`; cursor pagination) reads through the jurisdiction RLS of `cad_issue`.

Tables (all append-only, `archontos_app` SELECT/INSERT only): `cad_issue` (RLS),
`cad_issue_status`, `cad_issue_note`, `cad_issue_artifact` (RLS; generated bcf/report/markers
outputs linked to run, revision, action and issues).

## Resolution without `rerun_checks` (status `resolved`)

A superseding lineage diff (base = the run revision) resolves an issue when, deterministically:
the offending object was `removed`; or the newer revision has a check run for the same
rule_version (same jurisdiction, preferring the issue run reference date, then the newest run)
and that rule PASSes; or it still FAILs/REVIEWs but names offending objects and none of them is
a lineage successor (`cad_object_change.target_object_id`) of the issue object. A diff that
re-ran the checks (`rerun_checks`) resolves as before. Otherwise a superseded issue is `stale`
(also when the newer revision has no run for the rule or the offenders are unknown). Deriving
again appends a status row whenever the derived `(status, basis diff)` differs from the latest
one, so `stale` turns into `resolved` once the newer revision is checked.

## Issue notes (`src/archontos/cad/issue_notes.py`, migration `021`)

`POST /v1/cad/issues/{id}/notes {body}` (`cad.note`, 1..4000 chars) appends a note; the author
(`created_by`) is the request actor. `GET /v1/cad/issues/{id}/notes?limit&cursor` (`cad.read`)
lists them oldest first. Notes are append-only and read through the issue jurisdiction RLS.
Imported notes carry `kind` `bcf_comment` / `bcf_status` plus `external_guid`, `external_author`
and `external_at`.

## BCF import (`src/archontos/cad/bcf_import.py`)

`POST /v1/cad/issues/import.bcf[?run_id=|revision_id=]` (`cad.note`) takes a BCF 3.x zip as the
request body (max 20 MiB). Only `<guid>/markup.bcf` entries are read: topics are matched to
caller-visible issues by `topic_guid(issue_id)`; the topic status becomes a `bcf_status` note
(idempotent by status + modification date) and each comment (BCF 3.0 `Topic/Comments` or 2.1
`Markup/Comment`) a `bcf_comment` note idempotent by comment GUID. Unknown topics are listed in
`unknown_topic_guids`; issues are never created and the derived issue status is never changed by
an import. The zip is validated first (entry count, per-entry and total size, no absolute or
`..` names, no encryption, `bcf.version` 3.x) and XML is parsed with `defusedxml` (DTDs and
entities refused). Failures are 422; larger uploads 413.

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
   emits `cad.markers-exported` and marks the action succeeded, all in ONE transaction
   (row lock on the action; a failure leaves no output and the action stays approved). The original artifact is never
   written. The generic `POST /v1/actions/{id}/execute` refuses marker actions (409).

## Limits

* `to_power_cad_payload` key names have not been checked against the live power-cad
  `cad_create_many` schema; adapt before sending it to AutoCAD.
* Markers are DXF-only; IFC/SketchUp revisions get BCF instead.
