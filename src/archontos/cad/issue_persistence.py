"""PostgreSQL side of MVP-5 CAD issues (migration 019): derive, persist, list."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.check_persistence import CadCheckRepository
from archontos.cad.issues import ISSUE_DERIVER_VERSION, derive_issues, status_id_for
from archontos.cad.persistence import (
    CadRepository,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)


class RunNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DerivedIssues:
    run: dict[str, Any]
    issues: list[dict[str, Any]]
    new_issue_count: int
    new_status_count: int


_SUPERSEDING_SQL = """
    SELECT d.id::text AS id, d.target_revision_id::text AS target_revision_id,
           d.transitions_json AS transitions
      FROM cad_revision_diff d
      JOIN cad_revision tr ON tr.id = d.target_revision_id
      JOIN cad_revision br ON br.id = d.base_revision_id
     WHERE d.base_revision_id = CAST(:rev AS uuid) AND d.relation = 'lineage'
       AND tr.created_at >= br.created_at
       AND d.added_count + d.removed_count + d.modified_count > 0
     ORDER BY d.id
"""

_TARGET_SQL = """
    SELECT d.id::text AS id, d.base_revision_id::text AS base_revision_id,
           d.transitions_json AS transitions
      FROM cad_revision_diff d
     WHERE d.target_revision_id = CAST(:rev AS uuid)
     ORDER BY d.id
"""

_ISSUE_SELECT = """
    SELECT i.id::text AS id, i.run_id::text AS run_id, i.revision_id::text AS revision_id,
           i.rule_version_id::text AS rule_version_id, i.arch_object_id::text AS arch_object_id,
           i.object_kind, i.label, i.origin, i.diff_id::text AS diff_id, i.outcome,
           i.rule_title, i.reason, i.jurisdiction_code, i.reference_date, i.valid_from,
           i.valid_to, i.binding, i.evidence_locator_json AS evidence_locator, i.fact_paths,
           i.entity_handles, i.source_refs_json AS source_refs, i.bbox_json AS bbox,
           i.deriver_version, i.created_by, i.created_at, a.project_id,
           s.status, s.basis_diff_id::text AS status_basis_diff_id, s.derived_at AS status_at
      FROM cad_issue i
      JOIN cad_revision r ON r.id = i.revision_id
      JOIN cad_artifact a ON a.id = r.cad_artifact_id
      LEFT JOIN LATERAL (
        SELECT st.status, st.basis_diff_id, st.derived_at FROM cad_issue_status st
         WHERE st.issue_id = i.id ORDER BY st.derived_at DESC, st.id DESC LIMIT 1
      ) s ON true
"""


def _issue_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    for key in ("reference_date", "valid_from", "valid_to", "created_at", "status_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


class CadIssueRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _diffs(self, sql: str, rev: str) -> list[dict[str, Any]]:
        rows = (await self.session.execute(text(sql), {"rev": rev})).all()
        return [dict(r._mapping) for r in rows]

    async def _source_refs(self, rev: str) -> dict[str, list[tuple[str, str]]]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT s.arch_object_id::text AS oid, s.ref_kind, s.ref_value
                      FROM arch_object_source_ref s
                      JOIN arch_object o ON o.id = s.arch_object_id
                     WHERE o.revision_id = CAST(:rev AS uuid)
                    """
                ),
                {"rev": rev},
            )
        ).all()
        out: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            out.setdefault(r.oid, []).append((r.ref_kind, r.ref_value))
        return out

    async def derive(self, run_id: str) -> DerivedIssues:
        checks = CadCheckRepository(self.session)
        run = await checks.get_run(run_id)
        if run is None:
            raise RunNotFound("check run not found")
        rev = run["revision_id"]
        results = await checks.all_results(rev, run_id)
        objects = {o["id"]: o for o in await CadRepository(self.session).all_objects(rev)}
        issues = derive_issues(
            run,
            results,
            objects,
            await self._source_refs(rev),
            superseding_diffs=await self._diffs(_SUPERSEDING_SQL, rev),
            target_diffs=await self._diffs(_TARGET_SQL, rev),
        )
        new_issues = 0
        new_status = 0
        for issue in issues:
            inserted = (
                await self.session.execute(
                    text(
                        """
                        INSERT INTO cad_issue(id, run_id, revision_id, rule_version_id,
                            arch_object_id, object_kind, label, origin, diff_id, outcome,
                            rule_title, reason, jurisdiction_code, reference_date, valid_from,
                            valid_to, binding, evidence_locator_json, fact_paths,
                            entity_handles, source_refs_json, bbox_json, deriver_version)
                        VALUES (CAST(:id AS uuid), CAST(:run AS uuid), CAST(:rev AS uuid),
                            CAST(:rv AS uuid), CAST(:obj AS uuid), :kind, :label, :origin,
                            CAST(:diff AS uuid), :outcome, :title, :reason, :jur,
                            CAST(:ref AS date), CAST(:vf AS date), CAST(:vt AS date), :binding,
                            CAST(:evidence AS jsonb), :paths, :handles, CAST(:refs AS jsonb),
                            CAST(:bbox AS jsonb), :ver)
                        ON CONFLICT (id) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "id": issue["id"],
                        "run": issue["run_id"],
                        "rev": issue["revision_id"],
                        "rv": issue["rule_version_id"],
                        "obj": issue["arch_object_id"],
                        "kind": issue["object_kind"],
                        "label": issue["label"],
                        "origin": issue["origin"],
                        "diff": issue["diff_id"],
                        "outcome": issue["outcome"],
                        "title": issue["rule_title"],
                        "reason": issue["reason"],
                        "jur": issue["jurisdiction_code"] or run["jurisdiction_code"],
                        "ref": issue["reference_date"],
                        "vf": issue["valid_from"],
                        "vt": issue["valid_to"],
                        "binding": issue["binding"],
                        "evidence": json.dumps(issue["evidence_locator"], ensure_ascii=False),
                        "paths": issue["fact_paths"],
                        "handles": issue["entity_handles"],
                        "refs": json.dumps(issue["source_refs"], ensure_ascii=False),
                        "bbox": json.dumps(issue["bbox"]) if issue["bbox"] is not None else None,
                        "ver": ISSUE_DERIVER_VERSION,
                    },
                )
            ).first()
            new_issues += inserted is not None
            status_row = (
                await self.session.execute(
                    text(
                        """
                        INSERT INTO cad_issue_status(id, issue_id, status, basis_diff_id)
                        VALUES (CAST(:id AS uuid), CAST(:issue AS uuid), :status,
                                CAST(:basis AS uuid))
                        ON CONFLICT (id) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "id": status_id_for(
                            issue["id"], issue["status"], issue["status_basis_diff_id"]
                        ),
                        "issue": issue["id"],
                        "status": issue["status"],
                        "basis": issue["status_basis_diff_id"],
                    },
                )
            ).first()
            new_status += status_row is not None
        if new_issues or new_status:
            await self._emit(run, issues, new_issues, new_status)
        return DerivedIssues(run, issues, new_issues, new_status)

    async def _emit(
        self, run: dict[str, Any], issues: list[dict[str, Any]], new_issues: int, new_status: int
    ) -> None:
        counts: dict[str, int] = {}
        for i in issues:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        payload = {
            "cad_check_run_id": run["id"],
            "cad_revision_id": run["revision_id"],
            "jurisdiction_code": run["jurisdiction_code"],
            "issue_count": len(issues),
            "new_issue_count": new_issues,
            "new_status_count": new_status,
            "status_counts": counts,
            "regression_count": sum(1 for i in issues if i["origin"] == "regression"),
        }
        event_id: Any = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO domain_event(aggregate_type, aggregate_id, event_type,
                                             payload_json)
                    VALUES ('cad_check_run', :agg, 'CadIssuesDerived', CAST(:payload AS jsonb))
                    RETURNING event_id
                    """
                ),
                {"agg": run["id"], "payload": json.dumps(payload)},
            )
        ).scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (:event_id, 'cad.issues-derived', CAST(:payload AS jsonb))
                """
            ),
            {"event_id": event_id, "payload": json.dumps(payload)},
        )
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.issues.derive', 'cad_check_run', :id, CAST(:after AS jsonb))
                """
            ),
            {"id": run["id"], "after": json.dumps(payload, sort_keys=True)},
        )

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(_ISSUE_SELECT + " WHERE i.id = CAST(:id AS uuid)"), {"id": issue_id}
            )
        ).first()
        return _issue_row(row) if row else None

    async def list_issues(
        self,
        *,
        run_id: str | None,
        revision_id: str | None,
        status: str | None,
        outcome: str | None,
        rule_version_id: str | None,
        origin: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(
            run=run_id, r=revision_id, s=status, o=outcome, rv=rule_version_id, g=origin
        )
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    _ISSUE_SELECT
                    + """
                     WHERE (CAST(:run AS uuid) IS NULL OR i.run_id = CAST(:run AS uuid))
                       AND (CAST(:rev AS uuid) IS NULL OR i.revision_id = CAST(:rev AS uuid))
                       AND (CAST(:s AS text) IS NULL OR s.status = :s)
                       AND (CAST(:o AS text) IS NULL OR i.outcome = :o)
                       AND (CAST(:rv AS uuid) IS NULL OR i.rule_version_id = CAST(:rv AS uuid))
                       AND (CAST(:g AS text) IS NULL OR i.origin = :g)
                       AND (CAST(:after AS uuid) IS NULL OR i.id > CAST(:after AS uuid))
                     ORDER BY i.id LIMIT :lim
                    """
                ),
                {
                    "run": run_id,
                    "rev": revision_id,
                    "s": status,
                    "o": outcome,
                    "rv": rule_version_id,
                    "g": origin,
                    "after": after,
                    "lim": limit + 1,
                },
            )
        ).all()
        items = [_issue_row(r) for r in rows[:limit]]
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt

    async def all_issues(self, **filters: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            page, cursor = await self.list_issues(limit=500, cursor=cursor, **filters)
            out.extend(page)
            if cursor is None:
                return out
