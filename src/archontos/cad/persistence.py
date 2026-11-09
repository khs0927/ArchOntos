"""PostgreSQL persistence for parsed CAD revisions (migration 014). Append-only, idempotent."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.dxf import CAD_NAMESPACE, ParsedDrawing, sha256_hex, source_id_for, subject_ref
from archontos.cad.links import record_source_refs
from archontos.cad.model import building_facts
from archontos.storage.artifacts import ArtifactRef


class CadConflict(ValueError):
    """The drawing identity exists but is not visible/compatible (e.g. other jurisdiction)."""


class InvalidCadCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedRevision:
    cad_artifact_id: str
    revision_id: str
    created: bool
    revision: dict[str, Any]


def cad_artifact_id(project_id: str, source_key: str, sha256: str) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, f"art\n{source_id_for(project_id, source_key)}\n{sha256}"))


def revision_id(artifact_id: str, parser_revision: str) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, f"rev\n{artifact_id}\n{parser_revision}"))


def encode_cursor(last_id: str, fingerprint: str) -> str:
    raw = json.dumps({"a": last_id, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> str:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        last = str(UUID(str(data["a"])))
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCadCursor("malformed cursor") from exc
    if data.get("f") != fingerprint:
        raise InvalidCadCursor("cursor belongs to a different listing")
    return last


def filter_fingerprint(**filters: Any) -> str:
    return sha256_hex(json.dumps(filters, sort_keys=True, default=str))[:16]


_REVISION_SELECT = """
    SELECT r.id::text AS id, r.cad_artifact_id::text AS cad_artifact_id, r.revision_label,
           r.parser_revision, r.parser_revision_id, r.units, r.units_assumed, r.dxf_version,
           r.entity_count, r.object_count, r.unmapped_entity_count, r.content_digest,
           r.source_format, r.model_meta_json AS model_meta,
           r.created_by, r.created_at, a.project_id, a.source_key, a.source_id, a.sha256,
           a.file_name, a.jurisdiction_code, a.artifact_id::text AS artifact_id
      FROM cad_revision r JOIN cad_artifact a ON a.id = r.cad_artifact_id
"""


def _revision_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    out["created_at"] = out["created_at"].isoformat()
    return out


class CadRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _upsert_artifact(self, artifact: ArtifactRef) -> UUID:
        result = await self.session.execute(
            text(
                """
                INSERT INTO artifact(artifact_type, mime, storage_uri, content_hash, byte_size)
                VALUES (:artifact_type, :mime, :storage_uri, :content_hash, :byte_size)
                ON CONFLICT (storage_uri, content_hash)
                DO UPDATE SET byte_size = EXCLUDED.byte_size
                RETURNING id
                """
            ),
            {
                "artifact_type": artifact.artifact_type,
                "mime": artifact.mime,
                "storage_uri": artifact.storage_uri,
                "content_hash": artifact.content_hash,
                "byte_size": artifact.byte_size,
            },
        )
        return result.scalar_one()

    async def get_revision(self, rev_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(_REVISION_SELECT + " WHERE r.id = CAST(:id AS uuid)"), {"id": rev_id}
            )
        ).first()
        return _revision_row(row) if row else None

    async def persist(
        self,
        *,
        drawing: ParsedDrawing,
        artifact: ArtifactRef,
        project_id: str,
        source_key: str,
        jurisdiction_code: str,
        revision_label: str | None,
        file_name: str | None,
    ) -> PersistedRevision:
        if artifact.content_hash != drawing.sha256:
            raise CadConflict("stored artifact hash does not match the parsed bytes")
        art_id = cad_artifact_id(project_id, source_key, drawing.sha256)
        rev_id = revision_id(art_id, drawing.parser_revision)
        existing = await self.get_revision(rev_id)
        if existing is not None:
            return PersistedRevision(art_id, rev_id, False, existing)

        artifact_row = await self._upsert_artifact(artifact)
        await self.session.execute(
            text(
                """
                INSERT INTO cad_artifact(id, artifact_id, project_id, source_key, source_id,
                                         sha256, file_name, byte_size, jurisdiction_code,
                                         source_format)
                VALUES (CAST(:id AS uuid), :artifact_id, :project_id, :source_key, :source_id,
                        :sha256, :file_name, :byte_size, :jurisdiction_code, :source_format)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": art_id,
                "artifact_id": artifact_row,
                "project_id": project_id,
                "source_key": source_key,
                "source_id": source_id_for(project_id, source_key),
                "sha256": drawing.sha256,
                "file_name": file_name,
                "byte_size": drawing.byte_size,
                "jurisdiction_code": jurisdiction_code,
                "source_format": drawing.source_format,
            },
        )
        visible = (
            await self.session.execute(
                text("SELECT jurisdiction_code FROM cad_artifact WHERE id = CAST(:id AS uuid)"),
                {"id": art_id},
            )
        ).scalar_one_or_none()
        if visible is None:
            raise CadConflict("drawing exists in a jurisdiction this caller cannot access")
        if visible != jurisdiction_code:
            raise CadConflict(f"drawing is already filed under jurisdiction {visible}")

        await self.session.execute(
            text(
                """
                INSERT INTO cad_revision(id, cad_artifact_id, revision_label, parser_revision,
                    parser_revision_id, rule_set_json, units, units_assumed, dxf_version,
                    entity_count, object_count, unmapped_entity_count, content_digest,
                    source_format, model_meta_json)
                VALUES (CAST(:id AS uuid), CAST(:art AS uuid), :label, :pr, :prid,
                    CAST(:rules AS jsonb), :units, :assumed, :ver, :ec, :oc, :uc, :digest,
                    :source_format, CAST(:meta AS jsonb))
                """
            ),
            {
                "id": rev_id,
                "art": art_id,
                "label": revision_label or drawing.sha256[:12],
                "pr": drawing.parser_revision,
                "prid": sha256_hex(drawing.parser_revision),
                "rules": json.dumps(drawing.rule_set.as_dict(), ensure_ascii=False),
                "units": drawing.units,
                "assumed": drawing.units_assumed,
                "ver": drawing.dxf_version,
                "ec": len(drawing.entities),
                "oc": len(drawing.objects),
                "uc": drawing.unmapped_entity_count,
                "digest": drawing.content_digest,
                "source_format": drawing.source_format,
                "meta": json.dumps(drawing.model_meta, ensure_ascii=False),
            },
        )
        # Entity ids are scoped to the bytes; make them unique per revision row.
        ent_ids = {
            e.handle: str(uuid.uuid5(CAD_NAMESPACE, f"{rev_id}\n{e.id}")) for e in drawing.entities
        }
        if drawing.entities:
            await self.session.execute(
                text(
                    """
                    INSERT INTO cad_entity(id, revision_id, handle, layer, entity_type,
                        block_name, geometry_json, bbox_min_x, bbox_min_y, bbox_max_x,
                        bbox_max_y, entity_hash)
                    VALUES (CAST(:id AS uuid), CAST(:rev AS uuid), :handle, :layer, :type,
                        :block, CAST(:geom AS jsonb), :x0, :y0, :x1, :y1, :hash)
                    """
                ),
                [
                    {
                        "id": ent_ids[e.handle],
                        "rev": rev_id,
                        "handle": e.handle,
                        "layer": e.layer,
                        "type": e.entity_type,
                        "block": e.block_name,
                        "geom": json.dumps(e.geometry, ensure_ascii=False),
                        "x0": e.bbox[0] if e.bbox else None,
                        "y0": e.bbox[1] if e.bbox else None,
                        "x1": e.bbox[2] if e.bbox else None,
                        "y1": e.bbox[3] if e.bbox else None,
                        "hash": e.entity_hash,
                    }
                    for e in drawing.entities
                ],
            )
        if drawing.objects:
            obj_ids = {
                o.id: str(uuid.uuid5(CAD_NAMESPACE, f"{rev_id}\n{o.id}")) for o in drawing.objects
            }
            await self.session.execute(
                text(
                    """
                    INSERT INTO arch_object(id, revision_id, kind, rule_id, layer,
                        properties_json, fact_units_json, geometry_json, source_handles,
                        subject_ref_json, object_hash)
                    VALUES (CAST(:id AS uuid), CAST(:rev AS uuid), :kind, :rule, :layer,
                        CAST(:props AS jsonb), CAST(:units AS jsonb), CAST(:geom AS jsonb),
                        :handles, CAST(:ref AS jsonb), :hash)
                    """
                ),
                [
                    {
                        "id": obj_ids[o.id],
                        "rev": rev_id,
                        "kind": o.kind,
                        "rule": o.rule_id,
                        "layer": o.layer,
                        "props": json.dumps(o.properties, ensure_ascii=False),
                        "units": json.dumps(o.fact_units),
                        "geom": json.dumps(o.geometry),
                        "handles": list(o.source_handles),
                        "ref": json.dumps(
                            {
                                **subject_ref(
                                    o,
                                    project_id=project_id,
                                    source_key=source_key,
                                    drawing=drawing,
                                    revision_id=rev_id,
                                ),
                                "object_id": obj_ids[o.id],
                            },
                            ensure_ascii=False,
                        ),
                        "hash": o.object_hash,
                    }
                    for o in drawing.objects
                ],
            )
            links = [
                {
                    "obj": obj_ids[o.id],
                    "ent": ent_ids[h],
                    "role": "geometry" if i == 0 else "label",
                }
                for o in drawing.objects
                for i, h in enumerate(o.source_handles)
            ]
            await self.session.execute(
                text(
                    """
                    INSERT INTO arch_object_source(arch_object_id, cad_entity_id, role)
                    VALUES (CAST(:obj AS uuid), CAST(:ent AS uuid), :role)
                    """
                ),
                links,
            )
            await record_source_refs(
                self.session,
                drawing=drawing,
                object_ids=obj_ids,
                revision_id=rev_id,
                project_id=project_id,
                revision_label=revision_label or drawing.sha256[:12],
                jurisdiction_code=jurisdiction_code,
            )

        payload = {
            "cad_artifact_id": art_id,
            "cad_revision_id": rev_id,
            "project_id": project_id,
            "source_key": source_key,
            "sha256": drawing.sha256,
            "parser_revision": drawing.parser_revision,
            "object_count": len(drawing.objects),
        }
        if drawing.source_format != "dxf":
            payload["source_format"] = drawing.source_format
        event_id = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO domain_event(aggregate_type, aggregate_id, event_type,
                                             payload_json)
                    VALUES ('cad_revision', :agg, 'CadRevisionIngested',
                            CAST(:payload AS jsonb))
                    RETURNING event_id
                    """
                ),
                {"agg": rev_id, "payload": json.dumps(payload, ensure_ascii=False)},
            )
        ).scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (:event_id, 'cad.revision-ingested', CAST(:payload AS jsonb))
                """
            ),
            {"event_id": event_id, "payload": json.dumps(payload, ensure_ascii=False)},
        )
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.revision.ingest', 'cad_revision', :id, CAST(:after AS jsonb))
                """
            ),
            {"id": rev_id, "after": json.dumps(payload, sort_keys=True, ensure_ascii=False)},
        )
        created = await self.get_revision(rev_id)
        assert created is not None  # noqa: S101 - just inserted in this transaction
        return PersistedRevision(art_id, rev_id, True, created)

    async def list_revisions(
        self,
        *,
        project_id: str | None,
        source_key: str | None,
        sha256: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(p=project_id, s=source_key, h=sha256)
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    _REVISION_SELECT
                    + """
                     WHERE (CAST(:p AS text) IS NULL OR a.project_id = :p)
                       AND (CAST(:s AS text) IS NULL OR a.source_key = :s)
                       AND (CAST(:h AS text) IS NULL OR a.sha256 = :h)
                       AND (CAST(:after AS uuid) IS NULL OR r.id > CAST(:after AS uuid))
                     ORDER BY r.id LIMIT :lim
                    """
                ),
                {"p": project_id, "s": source_key, "h": sha256, "after": after, "lim": limit + 1},
            )
        ).all()
        items = [_revision_row(r) for r in rows[:limit]]
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt

    async def list_objects(
        self,
        rev_id: str,
        *,
        kind: str | None,
        layer: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(r=rev_id, k=kind, l=layer)
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT o.id::text AS id, o.kind, o.rule_id, o.layer,
                           o.properties_json AS properties, o.fact_units_json AS fact_units,
                           o.geometry_json AS geometry, o.source_handles,
                           o.subject_ref_json AS subject_ref, o.object_hash
                      FROM arch_object o
                      JOIN cad_revision r ON r.id = o.revision_id
                      JOIN cad_artifact a ON a.id = r.cad_artifact_id
                     WHERE o.revision_id = CAST(:rev AS uuid)
                       AND (CAST(:k AS text) IS NULL OR o.kind = :k)
                       AND (CAST(:l AS text) IS NULL OR o.layer = :l)
                       AND (CAST(:after AS uuid) IS NULL OR o.id > CAST(:after AS uuid))
                     ORDER BY o.id LIMIT :lim
                    """
                ),
                {"rev": rev_id, "k": kind, "l": layer, "after": after, "lim": limit + 1},
            )
        ).all()
        items = [dict(r._mapping) for r in rows[:limit]]
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt

    async def list_entities(
        self,
        rev_id: str,
        *,
        layer: str | None,
        entity_type: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(r=rev_id, l=layer, t=entity_type)
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT e.id::text AS id, e.handle, e.layer, e.entity_type, e.block_name,
                           e.geometry_json AS geometry, e.bbox_min_x, e.bbox_min_y,
                           e.bbox_max_x, e.bbox_max_y, e.entity_hash
                      FROM cad_entity e
                      JOIN cad_revision r ON r.id = e.revision_id
                      JOIN cad_artifact a ON a.id = r.cad_artifact_id
                     WHERE e.revision_id = CAST(:rev AS uuid)
                       AND (CAST(:l AS text) IS NULL OR e.layer = :l)
                       AND (CAST(:t AS text) IS NULL OR e.entity_type = :t)
                       AND (CAST(:after AS uuid) IS NULL OR e.id > CAST(:after AS uuid))
                     ORDER BY e.id LIMIT :lim
                    """
                ),
                {"rev": rev_id, "l": layer, "t": entity_type, "after": after, "lim": limit + 1},
            )
        ).all()
        items = []
        for r in rows[:limit]:
            d = dict(r._mapping)
            box = [d.pop(k) for k in ("bbox_min_x", "bbox_min_y", "bbox_max_x", "bbox_max_y")]
            d["bbox"] = box if box[0] is not None else None
            items.append(d)
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt

    async def all_objects(self, rev_id: str) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        cursor = None
        while True:
            page, cursor = await self.list_objects(
                rev_id, kind=None, layer=None, limit=500, cursor=cursor
            )
            objects.extend(page)
            if cursor is None:
                return objects


def facts_export(revision: dict[str, Any], objects: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a revision as ``aec-facts-export/1`` (accepted by integration.ontology)."""
    spaces = [o for o in objects if o["kind"] == "space"]
    walls = [o for o in objects if o["kind"] == "wall"]

    def total(items: list[dict[str, Any]], prop: str) -> float:
        return round(
            sum(o["properties"][prop]["value"] for o in items if prop in o["properties"]), 6
        )

    counts = {
        f"{kind}_count": sum(1 for o in objects if o["kind"] == kind)
        for kind in ("wall", "door", "window", "space", "stair", "column", "opening")
    }
    drawing = {
        **counts,
        "space_area_total": total(spaces, "area"),
        "wall_length_total": total(walls, "length"),
    }
    fact_units = {f"drawing.{k}": "count" for k in counts}
    fact_units["drawing.space_area_total"] = "m2"
    fact_units["drawing.wall_length_total"] = "m"
    slabs = sum(1 for o in objects if o["kind"] == "slab")
    if slabs:
        drawing["slab_count"] = slabs
        fact_units["drawing.slab_count"] = "count"
    facts: dict[str, Any] = {"drawing": drawing}
    building, building_units = building_facts(revision.get("model_meta"))
    facts.update(building)
    fact_units.update(building_units)
    source_format = revision.get("source_format") or "dxf"
    provenance = {
        path: {
            "method": f"archontos {source_format} parser",
            "cad_revision_id": revision["id"],
            "parser_revision": revision["parser_revision"],
            "coverage": "model" if path.startswith("building.") else "drawing",
        }
        for path in fact_units
    }
    return {
        "schema": "aec-facts-export/1",
        "project_key": revision["project_id"],
        "facts": {**facts, "fact_units": fact_units},
        "provenance": provenance,
        "subjects": [{"kind": o["kind"], "ref": o["subject_ref"]} for o in objects],
    }
