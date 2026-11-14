BEGIN;

-- P2: DXF <-> IFC geometry-based auto-linking (docs/MVP4-IFC-SKETCHUP.md).
-- model_object_link gains method 'geometric' and a confidence score (0..1, required exactly for
-- geometric links). Geometric links are computed at ingest between revisions of the same
-- project_id, jurisdiction and revision_label (one dxf, one ifc) by kind + overlap of the
-- unit-normalised (metre) geometry; objects that already have a manual or shared_ref link are
-- never auto-linked. Existing rows keep confidence NULL (no UPDATE; table is append-only).

ALTER TABLE model_object_link DROP CONSTRAINT model_object_link_method_check;
ALTER TABLE model_object_link ADD CONSTRAINT model_object_link_method_check
  CHECK (method IN ('shared_ref','manual','geometric'));

ALTER TABLE model_object_link
  ADD COLUMN confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  ADD CHECK ((method = 'geometric') = (confidence IS NOT NULL));

COMMIT;
