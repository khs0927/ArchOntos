"""MVP-5 generated outputs: compliance reports (report.py) and issue markers (markers.py).

Glue between the database (check runs, issues, diffs, evidence spans) and the two pure modules.
Every generated file is stored as a NEW content-addressed ``artifact`` and linked in
``cad_issue_artifact`` (migration 019) with an outbox event and an audit row. The original
drawing is only ever read.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.check_persistence import CadCheckRepository
from archontos.cad.dxf import CAD_NAMESPACE, UNIT_FACTORS
from archontos.cad.issue_persistence import CadIssueRepository
from archontos.cad.markers import DEFAULT_TEXT_HEIGHT, IssueMarker, MarkerError, build_marker_plan
from archontos.cad.report import (
    ComplianceReport,
    ReportDiff,
    ReportObject,
    ReportRule,
    ReportStale,
    ReportTransition,
)
from archontos.storage.artifacts import ArtifactRef

MARKER_ACTION_TYPE = "CadMarkersExport"


def output_id_for(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, "output\n" + kind + "\n" + "\n".join(parts)))


def _first_locator(locators: list[Any]) -> tuple[str | None, str | None]:
    if not locators:
        return None, None
    first = locators[0]
    if isinstance(first, dict):
        loc = first.get("locator", first)
        return json.dumps(loc, ensure_ascii=False, sort_keys=True), first.get("evidence_span_id")
    return str(first), None


async def build_report(
    session: AsyncSession,
    run: dict[str, Any],
    *,
    title: str,
    generated_at: str,
    diff_id: str | None,
) -> ComplianceReport:
    rev = run["revision_id"]
    results = await CadCheckRepository(session).all_results(rev, run["id"])
    objects = await _objects(session, rev)
    rules = []
    for r in results:
        locator, span_id = _first_locator(list(r.get("evidence_locator") or []))
        excerpt = None
        if span_id:
            excerpt = (
                await session.execute(
                    text("SELECT text_snippet FROM evidence_span WHERE id = CAST(:id AS uuid)"),
                    {"id": span_id},
                )
            ).scalar_one_or_none()
        objs = []
        for oid in r.get("arch_object_ids") or ():
            o = objects.get(str(oid))
            if o is None:
                continue
            handles = list(o["source_handles"] or [])
            objs.append(
                ReportObject(
                    kind=o["kind"],
                    handle=handles[0] if handles else None,
                    ifc_guid=o["ifc_guid"],
                )
            )
        rules.append(
            ReportRule(
                rule_version_id=r["rule_version_id"],
                rule_label=r["rule_title"],
                outcome=r["outcome"],
                jurisdiction_code=r["jurisdiction_code"],
                valid_from=r["effective"]["valid_from"],
                valid_to=r["effective"]["valid_to"],
                reason=r.get("reason"),
                evidence_locator=locator,
                article_excerpt=excerpt,
                objects=tuple(objs),
            )
        )
    diff = await _report_diff(session, diff_id) if diff_id else None
    return ComplianceReport(
        title=title,
        revision_id=rev,
        run_id=run["id"],
        jurisdiction_code=run["jurisdiction_code"],
        reference_date=run["reference_date"],
        generated_at=generated_at,
        rules=tuple(rules),
        not_applicable_count=int(run["not_applicable_count"]),
        stale_by_diff_ids=tuple(run.get("stale_by_diff_ids") or ()),
        diff=diff,
    )


async def _objects(session: AsyncSession, rev: str) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT o.id::text AS id, o.kind, o.source_handles,
                       (SELECT min(s.ref_value) FROM arch_object_source_ref s
                         WHERE s.arch_object_id = o.id AND s.ref_kind = 'ifc_guid') AS ifc_guid
                  FROM arch_object o WHERE o.revision_id = CAST(:rev AS uuid)
                """
            ),
            {"rev": rev},
        )
    ).all()
    return {r.id: dict(r._mapping) for r in rows}


class DiffNotFound(LookupError):
    pass


async def _report_diff(session: AsyncSession, diff_id: str) -> ReportDiff:
    row = (
        await session.execute(
            text(
                """
                SELECT base_revision_id::text AS base, target_revision_id::text AS target,
                       added_count, removed_count, modified_count, unchanged_count,
                       transitions_json AS transitions, stale_json AS stale
                  FROM cad_revision_diff WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": diff_id},
        )
    ).first()
    if row is None:
        raise DiffNotFound("CAD diff not found")
    stale = dict(row.stale or {})
    stale_items = (
        (ReportStale(diff_id=diff_id, reason="base check results superseded"),)
        if stale.get("stale_result_ids")
        else ()
    )
    return ReportDiff(
        base_revision_id=row.base,
        target_revision_id=row.target,
        added_count=row.added_count,
        removed_count=row.removed_count,
        modified_count=row.modified_count,
        unchanged_count=row.unchanged_count,
        transitions=tuple(
            ReportTransition(
                rule_version_id=t["rule_version_id"],
                rule_label=t.get("rule_title") or t["rule_version_id"],
                base_outcome=t.get("base_outcome"),
                target_outcome=t.get("target_outcome"),
            )
            for t in row.transitions or ()
        ),
        stale=stale_items,
    )


async def insert_artifact(session: AsyncSession, ref: ArtifactRef) -> str:
    result = await session.execute(
        text(
            """
            INSERT INTO artifact(artifact_type, mime, storage_uri, content_hash, byte_size)
            VALUES (:t, :m, :u, :h, :b)
            ON CONFLICT (storage_uri, content_hash) DO UPDATE SET byte_size = EXCLUDED.byte_size
            RETURNING id::text
            """
        ),
        {
            "t": ref.artifact_type,
            "m": ref.mime,
            "u": ref.storage_uri,
            "h": ref.content_hash,
            "b": ref.byte_size,
        },
    )
    return str(result.scalar_one())


_LINK_SELECT = """
    SELECT l.id::text AS id, l.kind, l.artifact_id::text AS artifact_id, l.run_id::text AS run_id,
           l.revision_id::text AS revision_id, l.action_id::text AS action_id,
           ARRAY(SELECT x::text FROM unnest(l.issue_ids) x) AS issue_ids, l.sha256, l.media_type,
           l.meta_json AS meta, l.jurisdiction_code, l.created_by, l.created_at,
           a.storage_uri
      FROM cad_issue_artifact l JOIN artifact a ON a.id = l.artifact_id
"""


def _link_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    out["created_at"] = out["created_at"].isoformat()
    return out


async def get_output(session: AsyncSession, output_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(_LINK_SELECT + " WHERE l.id = CAST(:id AS uuid)"), {"id": output_id}
        )
    ).first()
    return _link_row(row) if row else None


async def find_marker_output(session: AsyncSession, action_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                _LINK_SELECT
                + " WHERE l.kind = 'markers' AND l.action_id = CAST(:a AS uuid) ORDER BY l.id"
            ),
            {"a": action_id},
        )
    ).first()
    return _link_row(row) if row else None


async def record_output(
    session: AsyncSession,
    *,
    output_id: str,
    kind: str,
    artifact_id: str,
    run_id: str | None,
    revision_id: str,
    action_id: str | None,
    issue_ids: list[str],
    sha256: str,
    media_type: str,
    meta: dict[str, Any],
    jurisdiction_code: str,
    topic: str,
    event_type: str,
) -> tuple[dict[str, Any], bool]:
    """Insert the link row (idempotent by id) plus outbox + audit; (row, created)."""
    existing = await get_output(session, output_id)
    if existing is not None:
        return existing, False
    await session.execute(
        text(
            """
            INSERT INTO cad_issue_artifact(id, kind, artifact_id, run_id, revision_id, action_id,
                issue_ids, sha256, media_type, meta_json, jurisdiction_code)
            VALUES (CAST(:id AS uuid), :kind, CAST(:art AS uuid), CAST(:run AS uuid),
                CAST(:rev AS uuid), CAST(:act AS uuid), CAST(:issues AS uuid[]), :sha, :mt,
                CAST(:meta AS jsonb), :jur)
            """
        ),
        {
            "id": output_id,
            "kind": kind,
            "art": artifact_id,
            "run": run_id,
            "rev": revision_id,
            "act": action_id,
            "issues": issue_ids,
            "sha": sha256,
            "mt": media_type,
            "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "jur": jurisdiction_code,
        },
    )
    payload = {
        "output_id": output_id,
        "kind": kind,
        "artifact_id": artifact_id,
        "cad_check_run_id": run_id,
        "cad_revision_id": revision_id,
        "action_id": action_id,
        "sha256": sha256,
        "issue_count": len(issue_ids),
    }
    event_id: Any = (
        await session.execute(
            text(
                """
                INSERT INTO domain_event(aggregate_type, aggregate_id, event_type, payload_json)
                VALUES ('cad_issue_artifact', :agg, :et, CAST(:payload AS jsonb))
                RETURNING event_id
                """
            ),
            {"agg": output_id, "et": event_type, "payload": json.dumps(payload)},
        )
    ).scalar_one()
    await session.execute(
        text(
            """
            INSERT INTO outbox_message(event_id, topic, payload_json)
            VALUES (:event_id, :topic, CAST(:payload AS jsonb))
            """
        ),
        {"event_id": event_id, "topic": topic, "payload": json.dumps(payload)},
    )
    await session.execute(
        text(
            """
            INSERT INTO audit_log(action, target_table, target_id, after_json)
            VALUES (:action, 'cad_issue_artifact', :id, CAST(:after AS jsonb))
            """
        ),
        {"action": topic, "id": output_id, "after": json.dumps(payload, sort_keys=True)},
    )
    created = await get_output(session, output_id)
    assert created is not None  # noqa: S101 - inserted in this transaction
    return created, True


async def marker_plan_for_run(
    session: AsyncSession, run: dict[str, Any], revision: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Marker plan (drawing units) for the run's current ``open`` issues with geometry."""
    if revision.get("source_format", "dxf") != "dxf":
        raise MarkerError("markers can only be written to DXF revisions")
    units = str(revision["units"])
    if units not in DEFAULT_TEXT_HEIGHT or units not in UNIT_FACTORS:
        raise MarkerError(f"unsupported drawing units for markers: {units}")
    factor = float(UNIT_FACTORS[units])  # metres per drawing unit
    issues = await CadIssueRepository(session).all_issues(
        run_id=run["id"],
        revision_id=None,
        status="open",
        outcome=None,
        rule_version_id=None,
        origin=None,
    )
    markers = []
    for i in issues:
        if not i.get("bbox"):
            continue
        x0, y0, x1, y1 = (float(v) / factor for v in i["bbox"])
        handles = i.get("entity_handles") or []
        markers.append(
            IssueMarker(
                issue_id=i["id"],
                outcome=i["outcome"],
                rule_label=i["rule_title"],
                text=i.get("reason") or i["label"],
                units=units,
                entity_handle=handles[0] if handles else None,
                bbox=(x0, y0, x1, y1),
            )
        )
    if not markers:
        raise MarkerError("no open issues with geometry to mark")
    return build_marker_plan(markers), [m.issue_id for m in markers]


async def revision_source_uri(session: AsyncSession, revision_id: str) -> str | None:
    return (
        await session.execute(
            text(
                """
                SELECT f.storage_uri FROM cad_revision r
                  JOIN cad_artifact a ON a.id = r.cad_artifact_id
                  JOIN artifact f ON f.id = a.artifact_id
                 WHERE r.id = CAST(:rev AS uuid)
                """
            ),
            {"rev": revision_id},
        )
    ).scalar_one_or_none()
