BEGIN;

-- MVP-3 follow-up: wall split / merge in revision diffs (docs/MVP3-REVISION-DIFF.md).
-- cad_object_change  change_type gains 'split' (1 base wall -> N collinear target walls) and
--                    'merged' (N -> 1); both link a base and a target object, one row per link.
--                    match_method gains 'collinear'. lineage_json holds the whole group
--                    (base/target object ids, covered length, piece spans); '{}' otherwise.
-- cad_revision_diff  split_count / merged_count (rows), so "has changes" checks see them.
-- Existing rows keep their values through the column defaults (no UPDATE; tables are
-- append-only).

ALTER TABLE cad_object_change DROP CONSTRAINT cad_object_change_change_type_check;
ALTER TABLE cad_object_change ADD CONSTRAINT cad_object_change_change_type_check
  CHECK (change_type IN ('added','removed','modified','unchanged','split','merged'));

ALTER TABLE cad_object_change DROP CONSTRAINT cad_object_change_match_method_check;
ALTER TABLE cad_object_change ADD CONSTRAINT cad_object_change_match_method_check
  CHECK (match_method IN ('handle','content','geometry','collinear'));

ALTER TABLE cad_object_change DROP CONSTRAINT cad_object_change_check;
ALTER TABLE cad_object_change ADD CONSTRAINT cad_object_change_check
  CHECK (
    (change_type = 'added' AND base_object_id IS NULL AND target_object_id IS NOT NULL)
    OR (change_type = 'removed' AND base_object_id IS NOT NULL AND target_object_id IS NULL)
    OR (change_type IN ('modified','unchanged','split','merged')
        AND base_object_id IS NOT NULL AND target_object_id IS NOT NULL)
  );

ALTER TABLE cad_object_change
  ADD COLUMN lineage_json jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE cad_revision_diff
  ADD COLUMN split_count integer NOT NULL DEFAULT 0 CHECK (split_count >= 0),
  ADD COLUMN merged_count integer NOT NULL DEFAULT 0 CHECK (merged_count >= 0);

COMMIT;
