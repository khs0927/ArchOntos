"""Cross-source identity (MVP-4, migration 018).

Every ``arch_object`` records its source refs in ``arch_object_source_ref`` (``dxf_handle``,
``ifc_guid``, ``skp_persistent_id``). ``model_object_link`` links two objects of *different*
source formats that describe the same element of the same project revision:

* ``shared_ref``: created automatically at ingest when both carry the same ``ifc_guid`` (an IFC
  element, and a SketchUp entity whose IFC classification kept that GlobalId), within the same
  ``project_id`` and ``revision_label`` and jurisdiction;
* ``manual``: created through ``POST /v1/models/links``.

Links are append-only with deterministic ids (uuid5 of the ordered pair and method).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.dxf import CAD_NAMESPACE, ParsedDrawing
from archontos.cad.model import SHARED_REF_KINDS


class LinkInputError(ValueError):
    """The two objects cannot be linked (missing, same revision, other project...)."""


def link_id(object_a: str, object_b: str, method: str) -> str:
    a, b = sorted((object_a, object_b))
    return str(uuid.uuid5(CAD_NAMESPACE, f"link\n{a}\n{b}\n{method}"))


def object_refs(drawing: ParsedDrawing) -> dict[str, tuple[tuple[str, str], ...]]:
    """Source refs per parsed object id; DXF objects get their geometry handle."""
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    for obj in drawing.objects:
        if obj.source_refs:
            out[obj.id] = obj.source_refs
        elif drawing.source_format == "dxf" and obj.source_handles:
            out[obj.id] = (("dxf_handle", obj.source_handles[0]),)
    return out


async def record_source_refs(
    session: AsyncSession,
    *,
    drawing: ParsedDrawing,
    object_ids: dict[str, str],
    revision_id: str,
    project_id: str,
    revision_label: str,
    jurisdiction_code: str,
) -> int:
    """Insert source refs for a new revision and its shared-ref links; returns link count."""
    rows = [
        {"obj": object_ids[parsed_id], "kind": kind, "value": value}
        for parsed_id, refs in object_refs(drawing).items()
        for kind, value in refs
    ]
    if not rows:
        return 0
    await session.execute(
        text(
            """
            INSERT INTO arch_object_source_ref(arch_object_id, ref_kind, ref_value)
            VALUES (CAST(:obj AS uuid), :kind, :value)
            ON CONFLICT DO NOTHING
            """
        ),
        rows,
    )
    candidates = (
        await session.execute(
            text(
                """
                SELECT n.arch_object_id::text AS new_id, o.arch_object_id::text AS old_id,
                       oo.revision_id::text AS old_rev, n.ref_kind, n.ref_value
                  FROM arch_object_source_ref n
                  JOIN arch_object nob ON nob.id = n.arch_object_id
                  JOIN arch_object_source_ref o
                    ON o.ref_kind = n.ref_kind AND o.ref_value = n.ref_value
                   AND o.arch_object_id <> n.arch_object_id
                  JOIN arch_object oo ON oo.id = o.arch_object_id
                  JOIN cad_revision orv ON orv.id = oo.revision_id
                  JOIN cad_artifact oa ON oa.id = orv.cad_artifact_id
                 WHERE nob.revision_id = CAST(:rev AS uuid)
                   AND n.ref_kind = ANY(CAST(:kinds AS text[]))
                   AND oa.project_id = :project
                   AND oa.jurisdiction_code = :jur
                   AND orv.revision_label = :label
                   AND orv.source_format <> :fmt
                 ORDER BY 1, 2
                """
            ),
            {
                "rev": revision_id,
                "kinds": list(SHARED_REF_KINDS),
                "project": project_id,
                "jur": jurisdiction_code,
                "label": revision_label,
                "fmt": drawing.source_format,
            },
        )
    ).all()
    links = []
    for row in candidates:
        a, b = sorted(((row.new_id, revision_id), (row.old_id, row.old_rev)))
        links.append(
            {
                "id": link_id(a[0], b[0], "shared_ref"),
                "project": project_id,
                "jur": jurisdiction_code,
                "a": a[0],
                "b": b[0],
                "ra": a[1],
                "rb": b[1],
                "kind": row.ref_kind,
                "value": row.ref_value,
            }
        )
    if links:
        await session.execute(
            text(
                """
                INSERT INTO model_object_link(id, project_id, jurisdiction_code, object_a_id,
                    object_b_id, revision_a_id, revision_b_id, method, ref_kind, ref_value)
                VALUES (CAST(:id AS uuid), :project, :jur, CAST(:a AS uuid), CAST(:b AS uuid),
                    CAST(:ra AS uuid), CAST(:rb AS uuid), 'shared_ref', :kind, :value)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            links,
        )
    return len(links)


_OBJECT_SIDE = """
    SELECT o.id::text AS id, o.kind, o.revision_id::text AS revision_id, r.source_format,
           r.revision_label, a.project_id, a.jurisdiction_code, o.source_handles
      FROM arch_object o
      JOIN cad_revision r ON r.id = o.revision_id
      JOIN cad_artifact a ON a.id = r.cad_artifact_id
     WHERE o.id = CAST(:id AS uuid)
"""


@dataclass(frozen=True, slots=True)
class PersistedLink:
    created: bool
    link: dict[str, Any]


def _link_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    out["created_at"] = out["created_at"].isoformat()
    return out


_LINK_SELECT = """
    SELECT l.id::text AS id, l.project_id, l.jurisdiction_code, l.method, l.ref_kind,
           l.ref_value, l.note, l.created_by, l.created_at,
           l.object_a_id::text AS object_a_id, l.revision_a_id::text AS revision_a_id,
           oa.kind AS object_a_kind, ra.source_format AS object_a_source_format,
           oa.source_handles AS object_a_handles,
           l.object_b_id::text AS object_b_id, l.revision_b_id::text AS revision_b_id,
           ob.kind AS object_b_kind, rb.source_format AS object_b_source_format,
           ob.source_handles AS object_b_handles
      FROM model_object_link l
      JOIN arch_object oa ON oa.id = l.object_a_id
      JOIN cad_revision ra ON ra.id = oa.revision_id
      JOIN cad_artifact aa ON aa.id = ra.cad_artifact_id
      JOIN arch_object ob ON ob.id = l.object_b_id
      JOIN cad_revision rb ON rb.id = ob.revision_id
      JOIN cad_artifact ab ON ab.id = rb.cad_artifact_id
"""


class ModelLinkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, lid: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(_LINK_SELECT + " WHERE l.id = CAST(:id AS uuid)"), {"id": lid}
            )
        ).first()
        return _link_row(row) if row else None

    async def create_manual(
        self, *, object_a_id: str, object_b_id: str, note: str | None
    ) -> PersistedLink:
        sides = []
        for oid in (object_a_id, object_b_id):
            row = (await self.session.execute(text(_OBJECT_SIDE), {"id": oid})).first()
            if row is None:
                raise LookupError(f"object {oid} not found")
            sides.append(row)
        a, b = sorted(sides, key=lambda r: r.id)
        if a.id == b.id or a.revision_id == b.revision_id:
            raise LinkInputError("link objects of two different revisions")
        if a.project_id != b.project_id:
            raise LinkInputError("both objects must belong to the same project_id")
        if a.jurisdiction_code != b.jurisdiction_code:
            raise LinkInputError("both objects must be filed under the same jurisdiction")
        lid = link_id(a.id, b.id, "manual")
        existing = await self.get(lid)
        if existing is not None:
            return PersistedLink(False, existing)
        await self.session.execute(
            text(
                """
                INSERT INTO model_object_link(id, project_id, jurisdiction_code, object_a_id,
                    object_b_id, revision_a_id, revision_b_id, method, note)
                VALUES (CAST(:id AS uuid), :project, :jur, CAST(:a AS uuid), CAST(:b AS uuid),
                    CAST(:ra AS uuid), CAST(:rb AS uuid), 'manual', :note)
                """
            ),
            {
                "id": lid,
                "project": a.project_id,
                "jur": a.jurisdiction_code,
                "a": a.id,
                "b": b.id,
                "ra": a.revision_id,
                "rb": b.revision_id,
                "note": note,
            },
        )
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.model-link.create', 'model_object_link', :id,
                        CAST(:after AS jsonb))
                """
            ),
            {
                "id": lid,
                "after": json.dumps(
                    {"object_a_id": a.id, "object_b_id": b.id, "method": "manual"},
                    sort_keys=True,
                ),
            },
        )
        created = await self.get(lid)
        assert created is not None  # noqa: S101 - just inserted in this transaction
        return PersistedLink(True, created)

    async def list_links(
        self,
        *,
        project_id: str | None,
        revision_id: str | None,
        object_id: str | None,
        method: str | None,
        limit: int,
        after: str | None,
    ) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                text(
                    _LINK_SELECT
                    + """
                     WHERE (CAST(:p AS text) IS NULL OR l.project_id = :p)
                       AND (CAST(:r AS uuid) IS NULL
                            OR l.revision_a_id = CAST(:r AS uuid)
                            OR l.revision_b_id = CAST(:r AS uuid))
                       AND (CAST(:o AS uuid) IS NULL
                            OR l.object_a_id = CAST(:o AS uuid)
                            OR l.object_b_id = CAST(:o AS uuid))
                       AND (CAST(:m AS text) IS NULL OR l.method = :m)
                       AND (CAST(:after AS uuid) IS NULL OR l.id > CAST(:after AS uuid))
                     ORDER BY l.id LIMIT :lim
                    """
                ),
                {
                    "p": project_id,
                    "r": revision_id,
                    "o": object_id,
                    "m": method,
                    "after": after,
                    "lim": limit,
                },
            )
        ).all()
        return [_link_row(r) for r in rows]
