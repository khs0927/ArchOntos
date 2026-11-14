"""MVP-5 CAD issues: one issue per (rule_version, offending arch_object) of a check run.

Issues are derived, never typed in: every FAIL/REVIEW ``cad_check_result`` of a run yields one
issue per offending ``arch_object`` (or one drawing-level issue when the result names no
object). Regressions found by an MVP-3 diff re-run (``PASS->FAIL`` etc. with the run on the
diff's *target* revision) are marked ``origin = "regression"`` with the diff id.

Status is derived from newer revisions, never carried over silently:

* ``open``      the run's revision has no superseding lineage diff (with changes);
* ``resolved``  a superseding diff shows the rule passing: either it re-ran this run checks
                (``rerun_checks``), or the target revision has its own check run for the same
                rule_version and, following the diff lineage, the offending object was removed,
                the rule PASSes, or none of the object successors is still an offender;
* ``stale``     the revision was superseded, but nothing above shows the issue gone. The target
                revision's own run carries its own (new) issues.

Ids are deterministic (uuid5), so deriving twice gives the same issues; a status row is only
appended when the derived status (and its basis) is new.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from archontos.cad.dxf import CAD_NAMESPACE

ISSUE_DERIVER_VERSION = "cad-issues/1"
ISSUE_OUTCOMES = ("FAIL", "REVIEW")
ISSUE_STATUSES = ("open", "resolved", "stale")


def issue_id_for(run_id: str, rule_version_id: str, arch_object_id: str | None) -> str:
    return str(
        uuid.uuid5(CAD_NAMESPACE, f"issue\n{run_id}\n{rule_version_id}\n{arch_object_id or '-'}")
    )


def status_id_for(issue_id: str, status: str, basis_diff_id: str | None, seq: int = 0) -> str:
    """Deterministic status row id; ``seq`` = number of earlier rows of the issue (0 = first)."""
    suffix = f"\n{seq}" if seq else ""
    return str(
        uuid.uuid5(CAD_NAMESPACE, f"issue-status\n{issue_id}\n{status}\n{basis_diff_id}{suffix}")
    )


def _points(geometry: Mapping[str, Any] | None) -> list[tuple[float, float]]:
    if not geometry:
        return []
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return []
    if isinstance(coords[0], (int, float)):
        coords = [coords]
    out: list[tuple[float, float]] = []
    for c in coords:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                out.append((float(c[0]), float(c[1])))
            except (TypeError, ValueError):
                continue
    return out


def geometry_bbox(geometry: Mapping[str, Any] | None) -> list[float] | None:
    """Plan bbox ``[x0, y0, x1, y1]`` in metres of an arch_object geometry (always metres)."""
    pts = _points(geometry)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def object_label(obj: Mapping[str, Any]) -> str:
    props = obj.get("properties") or {}
    name = props.get("name") if isinstance(props, Mapping) else None
    return f"{obj.get('kind', 'object')}:{name}" if name else str(obj.get("kind", "object"))


def _resolved_by_lineage(
    diff: Mapping[str, Any], rule_version_id: str, arch_object_id: str | None
) -> bool:
    """Lineage-based resolution against one superseding diff (no ``rerun_checks`` needed).

    ``diff["changes"]`` maps a base ``arch_object`` id to ``{"change_type", "target_object_ids"}``
    (from ``cad_object_change``); ``diff["target_results"]`` maps a rule_version id to the
    ``{"outcome", "arch_object_ids"}`` of the check run on the diff target revision.

    * the offending object was removed -> resolved;
    * the target revision has a run for the rule and it PASSes -> resolved;
    * it still FAILs/REVIEWs but names offending objects and none of them is a lineage successor
      of this issue object -> resolved (the object itself now passes);
    * anything else (no run for the rule, object still offending, offenders unknown) -> not
      resolved.
    """
    change = (diff.get("changes") or {}).get(arch_object_id) if arch_object_id else None
    if change and change.get("change_type") == "removed":
        return True
    result = (diff.get("target_results") or {}).get(rule_version_id)
    if not result:
        return False
    if result.get("outcome") == "PASS":
        return True
    offenders = {str(x) for x in result.get("arch_object_ids") or ()}
    successors = {str(x) for x in (change or {}).get("target_object_ids") or ()}
    return bool(change and offenders and successors and not (offenders & successors))


def derive_status(
    run_id: str,
    rule_version_id: str,
    superseding_diffs: Iterable[Mapping[str, Any]],
    arch_object_id: str | None = None,
) -> tuple[str, str | None]:
    """(status, basis diff id) for one issue of ``run_id``.

    ``superseding_diffs`` are lineage diffs with changes whose *base* is the run revision.
    A diff resolves the issue either because it re-ran this run checks (``rerun_checks``) and
    the rule PASSes on the target, or by lineage (:func:`_resolved_by_lineage`); the first
    resolving diff (by id) is the basis. Otherwise a superseded issue is ``stale``.
    """
    stale_by: str | None = None
    for diff in sorted(superseding_diffs, key=lambda d: str(d["id"])):
        for t in diff.get("transitions") or ():
            if t.get("base_run_id") == run_id and t.get("rule_version_id") == rule_version_id:
                if t.get("target_outcome") == "PASS":
                    return "resolved", str(diff["id"])
        if _resolved_by_lineage(diff, rule_version_id, arch_object_id):
            return "resolved", str(diff["id"])
        if stale_by is None:
            stale_by = str(diff["id"])
    if stale_by is not None:
        return "stale", stale_by
    return "open", None


def _regressions(
    run_id: str, target_diffs: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for diff in sorted(target_diffs, key=lambda d: str(d["id"])):
        for t in diff.get("transitions") or ():
            if t.get("target_run_id") == run_id and t.get("regression"):
                out.setdefault(
                    str(t["rule_version_id"]),
                    {"diff_id": str(diff["id"]), "transition": t.get("transition")},
                )
    return out


def derive_issues(
    run: Mapping[str, Any],
    results: Iterable[Mapping[str, Any]],
    objects: Mapping[str, Mapping[str, Any]],
    source_refs: Mapping[str, list[tuple[str, str]]],
    *,
    superseding_diffs: Iterable[Mapping[str, Any]] = (),
    target_diffs: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Derive the issues of one check run (sorted by id; pure, deterministic).

    ``run`` needs ``id``, ``revision_id``, ``reference_date``; ``results`` are the run's
    ``cad_check_result`` views (``result_view`` shape); ``objects`` are the revision's
    ``arch_object`` rows by id; ``source_refs`` maps object id -> ``[(ref_kind, value)]``.
    """
    run_id = str(run["id"])
    superseding = list(superseding_diffs)
    regressions = _regressions(run_id, target_diffs)
    issues: dict[str, dict[str, Any]] = {}
    for result in results:
        if result.get("outcome") not in ISSUE_OUTCOMES:
            continue
        rv = str(result["rule_version_id"])
        effective = result.get("effective") or {
            "valid_from": result.get("valid_from"),
            "valid_to": result.get("valid_to"),
        }
        object_ids: list[str | None] = [
            *sorted({str(x) for x in result.get("arch_object_ids") or ()})
        ] or [None]
        regression = regressions.get(rv)
        result_handles = [str(h) for h in result.get("entity_handles") or ()]
        for oid in object_ids:
            obj = objects.get(oid) if oid else None
            if obj:
                own = {str(h) for h in obj.get("source_handles") or ()}
                handles = sorted(set(result_handles) & own) or sorted(own)
            else:
                handles = sorted(set(result_handles))
            refs = sorted(source_refs.get(oid, [])) if oid else []
            iid = issue_id_for(run_id, rv, oid)
            status, basis = derive_status(run_id, rv, superseding, oid)
            issues[iid] = {
                "id": iid,
                "run_id": run_id,
                "revision_id": str(run["revision_id"]),
                "rule_version_id": rv,
                "rule_title": result.get("rule_title") or "",
                "arch_object_id": oid,
                "object_kind": obj.get("kind") if obj else None,
                "label": object_label(obj) if obj else "drawing",
                "origin": "regression" if regression else "check",
                "diff_id": regression["diff_id"] if regression else None,
                "outcome": result["outcome"],
                "reason": result.get("reason"),
                "jurisdiction_code": result.get("jurisdiction_code"),
                "reference_date": str(run["reference_date"]),
                "valid_from": effective.get("valid_from"),
                "valid_to": effective.get("valid_to"),
                "binding": bool(result.get("binding")),
                "evidence_locator": list(result.get("evidence_locator") or []),
                "fact_paths": [str(p) for p in result.get("fact_paths") or ()],
                "entity_handles": handles,
                "source_refs": [{"kind": k, "value": v} for k, v in refs],
                "bbox": geometry_bbox(obj.get("geometry")) if obj else None,
                "status": status,
                "status_basis_diff_id": basis,
            }
    return [issues[k] for k in sorted(issues)]


def ifc_guids(issue: Mapping[str, Any]) -> list[str]:
    return [r["value"] for r in issue.get("source_refs") or () if r.get("kind") == "ifc_guid"]
