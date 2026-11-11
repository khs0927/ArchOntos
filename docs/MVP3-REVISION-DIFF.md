# MVP-3: permit vs construction / revision diff

MVP-3 compares two `cad_revision`s and stores an append-only, deterministic diff. The two
revisions can be successive revisions of one drawing (same project and source key) or any two
explicitly paired ones, for example the permit set and the construction set. Everything is
file/JSON based and makes no network calls.

Code: `archontos.cad.diff` (pure), `archontos.cad.diff_persistence` (PostgreSQL),
`apps/ingestion.py` (API), migration `017_cad_revision_diff.sql`.

## 1. Matching

Objects are matched within one kind (`wall`, `door`, `window`, `space`, `stair`, `column`,
`opening`):

1. **Content match.** Objects with identical layer, properties and geometry are paired first,
   preferring equal handles (`match_method` is `handle` when the handles agree, else `content`).
   This stops reassigned handles from pairing an object with a look-alike.
2. **Handle match.** Same kind and the same DXF source handles, with exactly one candidate on
   each side. It is refused when the objects do not overlap and are more than
   `handle_max_shift` apart (handles reassigned by another tool). Disable with
   `match_by_handle: false`.
3. **Geometric match** for the rest. A pair is a candidate when the bbox IoU is at least
   `match_min_iou` or the centroid distance is at most `match_centroid`. Pairs are assigned
   greedily, best IoU first, ties by distance and id. Line-like objects get a 1 cm padding so
   walls have a footprint. A grid index keeps this fast for large drawings.
4. What is left on the base side is `removed`, what is left on the target side is `added`.

**Wall split and merge** (before steps 2 and 3, walls only, `detect_split: true`). Straight walls
left after the content match are tested:

- **split** (1 -> N): N >= 2 target walls that are parallel to one base wall (within 1 degree),
  lie on its axis (every end point within `split_offset`), stay within its extent and cover it
  with no gap (at the ends or between pieces) larger than `split_max_gap`, and do not overlap
  each other.
- **merged** (N -> 1): the same test with the roles swapped.

Base walls are tried in id order for splits, then target walls in id order for merges; a wall
joins at most one group. Each link of a group is one change row with `change_type` `split` or
`merged`, `match_method` `collinear`, both object ids, and `match_score` = piece length / whole
length. Every row of a group carries the same `lineage`: `group_type`, `base_object_ids`,
`target_object_ids`, `whole_length`, `covered_length` and `pieces` (`object_id`, `start`, `end`
along the whole wall, in metres). A BREAK that keeps the original handle on one piece is still a
split, not a modified wall plus an added one. A wall split around a new door opening leaves a
gap of the door width; raise `split_max_gap` (e.g. 1.2 m) to report it as a split.

A matched pair is `unchanged` when its `object_hash` is equal or when every delta is within
tolerance. Otherwise it is `modified`. `match_method` (`handle`, `content` or `geometry`) and
`match_score` are stored.

## 2. Deltas and tolerances

| Delta `change` | Meaning |
|---|---|
| `numeric` | a quantity changed: `base`, `target`, `delta` and `unit` (m, m2, deg) |
| `unit` | the two sides use different units; no delta is computed |
| `value` | a text property (for example the room name) or the layer changed |
| `added` / `removed` | the property exists on one side only |
| `geometry` | `geometry.centroid` (shift, `dx`, `dy`, `delta` in m) or `geometry.bbox_size` |

Each delta has a `fact_path` (`door.width`, `space.area`) where one exists, so it lines up with
MVP-2 facts.

Tolerances (request field `tolerances`, all optional, all validated):

| Key | Default | Use |
|---|---|---|
| `numeric_abs` | 0.001 | absolute tolerance in the property's own unit |
| `numeric_rel` | 0 | relative tolerance; the larger of the two applies |
| `per_property` | `{}` | overrides `numeric_abs`, by `kind.property` or `property` |
| `position` | 0.001 m | centroid shift and bbox size change |
| `match_centroid` | 0.5 m | geometric matching distance |
| `match_min_iou` | 0.5 | geometric matching overlap (0 to 1) |
| `handle_max_shift` | 10 m | sanity limit for handle matches |
| `match_by_handle` | true | |
| `split_offset` | 0.01 m | max distance of a split/merge piece from the whole wall's axis |
| `split_max_gap` | 0.05 m | max uncovered gap at the ends or between pieces |
| `detect_split` | true | turn split/merge detection off |

Unknown keys, negative or non-finite values give 422.

## 3. Impact and the stale rule

For every changed object, the diff lists the base-revision check results it affects
(`impact.base`), and the target-revision results when checks were re-run (`impact.target`). A
result is affected when its `arch_object_ids` contain the object (`via: object`) or when its
`fact_paths` read the changed property or kind (`via: fact`). For an added, removed, split or merged object
this includes drawing-level facts such as `drawing.space_area_total`.

**Stale edge rule** (see ONTOLOGY-INTEGRATION.md): results are bound to the revision they were
computed on and are never carried over.

- Every base result listed in `impact.base` carries `stale: true`. `stale` on the diff names
  the base check runs, the stale result ids and `carried_over: false`. `target_unchecked`
  says the target revision has no check run yet.
- Check runs and results on a revision that is the base of a lineage diff with at least one
  change now report `stale: true` and `stale_by_diff_ids` in the existing `GET
  /v1/cad/revisions/{id}/checks[/results]` responses.
- A diff of two paired (unrelated) drawings reports impact but does not mark either side stale.

## 4. Re-running checks

With `rerun_checks: true`, every check run of the base revision is evaluated again on the
target revision: same jurisdiction, reference date, land parcel and caller facts (recovered
from the stored run), same rule versions. The new runs are ordinary idempotent MVP-2 runs. The
diff stores one transition per rule: `base_outcome`, `target_outcome`, `transition`
(`PASS->FAIL`, `REVIEW->N/A`), `changed`, `regression` (worse by PASS < REVIEW < FAIL),
`improvement` and the `change_ids` that touch that rule. Without `rerun_checks`,
`transitions` is empty.

## Schema (migrations 017, 020)

| Table | Identity | Notes |
|---|---|---|
| `cad_revision_diff` | uuid5(base, target, tolerances digest, rerun flag, differ version) | `relation` (`lineage` or `paired`), `jurisdiction_code` (RLS), tolerances, counts (020 adds `split_count`, `merged_count`), `summary_json`, `transitions_json`, `stale_json` |
| `cad_object_change` | uuid5(diff, type, base object, target object) | `change_type` (added, removed, modified, unchanged, split, merged), `kind`, object ids, `match_method` (handle, content, geometry, collinear), `match_score`, handles, `deltas_json`, `impact_json`, `lineage_json` (020) |

- Both tables are append-only (`cad_reject_mutation`); `archontos_app` has SELECT and INSERT only.
  `cad_revision_diff` has the same fail-closed jurisdiction policy as `cad_artifact`; changes
  are read through it.
- Repeating a request with the same revisions, tolerances and rerun flag returns the existing
  diff (HTTP 200, `created: false`). Impact and transitions are a snapshot of the check runs
  that existed when the diff was created.
- A new diff writes a `domain_event` `CadRevisionDiffed`, an `outbox_message` on topic
  `cad.revision-diffed` and an `audit_log` row `cad.revision.diff`.
- Both revisions must be visible to the caller and in the same jurisdiction.

## API (ingestion service)

| Method | Path | Permission |
|---|---|---|
| POST | `/v1/cad/diffs`, body `{base_revision_id, target_revision_id, tolerances?, rerun_checks?}` | `cad.diff` (curator, reviewer, admin); `rerun_checks` also needs `cad.check` |
| GET | `/v1/cad/diffs/{id}` | `cad.diff` |
| GET | `/v1/cad/diffs/{id}/changes?change_type=&kind=&limit=&cursor=` | `cad.diff` |

| Status | When |
|---|---|
| 400 | a cursor is reused with different filters |
| 404 | a revision or diff is outside the caller's jurisdictions or does not exist |
| 422 | same revision twice, different jurisdictions, invalid tolerances, unusable stored check inputs |

## Tests

- `tests/test_cad_diff.py`: DXF pairs built in code (`revision_plan` in `tests/cad_fixtures.py`):
  moved door (by handle and by geometry), widened corridor with units, removed wall, added space,
  unchanged, tolerances, determinism, stale impact and a PASS to FAIL transition.
- `tests/integration/test_cad_diff_postgres.py` (CI): two uploaded revisions, a real active rule,
  re-run with transition, stale flags, idempotency, filters, pagination, append-only, RLS.

## Known limits

- Split/merge detection covers straight walls only (not polylines with corners, spaces or
  slabs) and pieces that stay on the original axis; a wall split and moved is remove plus add.
- Centroids are bbox centres; rotation without a bbox change is seen only through properties.
- Impact is a snapshot taken at diff time. Checks run later are not added to an existing diff.
- No Ontology export of stale edges yet. `stale` is available through the API only.
