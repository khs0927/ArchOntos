from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.ingestion.adapters import LawBody, LawSearchItem
from archontos.storage.artifacts import ArtifactRef


class CanonicalizationError(ValueError):
    pass


class SourceVersionConflict(CanonicalizationError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedLawVersion:
    source_id: UUID
    source_version_id: UUID
    artifact_id: UUID
    created: bool
    conflict: bool = False


def classify_document_type(law_type: str) -> str:
    normalized = law_type.strip()
    if normalized in {"헌법", "법률"}:
        return "statute"
    if normalized == "대통령령":
        return "regulation"
    if "부령" in normalized or normalized in {"총리령", "규칙"}:
        return "rule"
    if "조례" in normalized:
        return "ordinance"
    return "guide"


def law_source_key(item: LawSearchItem) -> str:
    if not item.law_id:
        raise CanonicalizationError("law.go.kr item is missing law_id")
    return f"lawgo:law:{item.law_id}"


def law_version_label(item: LawSearchItem) -> str:
    if item.mst:
        return f"mst:{item.mst}"
    if item.enforcement_date:
        return f"effective:{item.enforcement_date.isoformat()}"
    raise CanonicalizationError("law.go.kr item has neither MST nor enforcement date")


class CanonicalLawRepository:
    """PostgreSQL writes for an immutable official-law source version."""

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

    async def persist_law_version(
        self,
        *,
        item: LawSearchItem,
        body: LawBody,
        body_artifact: ArtifactRef,
        discovery_artifact: ArtifactRef | None = None,
    ) -> PersistedLawVersion:
        effective_from = item.enforcement_date or body.enforcement_date
        if effective_from is None:
            raise CanonicalizationError("official law version is missing an enforcement date")

        source_key = law_source_key(item)
        version_label = law_version_label(item)
        document_type = classify_document_type(item.law_type or body.law_type)
        detail_url = item.detail_link
        if detail_url.startswith("/"):
            detail_url = f"https://www.law.go.kr{detail_url}"

        source_result = await self.session.execute(
            text(
                """
                INSERT INTO source_document(
                    source_key, title, issuer, jurisdiction_code, document_type, source_url
                )
                VALUES (:source_key, :title, :issuer, 'KR', :document_type, :source_url)
                ON CONFLICT (source_key) DO UPDATE SET
                    title = EXCLUDED.title,
                    issuer = EXCLUDED.issuer,
                    document_type = EXCLUDED.document_type,
                    source_url = EXCLUDED.source_url
                RETURNING id
                """
            ),
            {
                "source_key": source_key,
                "title": item.law_name or body.law_name,
                "issuer": item.ministry or body.ministry or "대한민국",
                "document_type": document_type,
                "source_url": detail_url or None,
            },
        )
        source_id: UUID = source_result.scalar_one()
        artifact_id = await self._upsert_artifact(body_artifact)
        discovery_artifact_id = None
        if discovery_artifact is not None:
            discovery_artifact_id = await self._upsert_artifact(discovery_artifact)

        manifest: dict[str, Any] = {
            "source": "law.go.kr",
            "law_id": item.law_id,
            "mst": item.mst,
            "law_type": item.law_type,
            "ministry": item.ministry,
            "history_code": item.history_code,
            "revision_type": item.revision_type,
            "body_sha256": body_artifact.content_hash,
        }
        if discovery_artifact is not None:
            manifest["discovery_sha256"] = discovery_artifact.content_hash
            manifest["discovery_artifact_id"] = str(discovery_artifact_id)

        inserted = await self.session.execute(
            text(
                """
                INSERT INTO source_version(
                    source_id, artifact_id, version_label, effective_from, promulgated_at,
                    raw_manifest_json, status
                )
                VALUES (
                    :source_id, :artifact_id, :version_label, :effective_from, :promulgated_at,
                    CAST(:raw_manifest_json AS jsonb), 'published'
                )
                ON CONFLICT (source_id, version_label) DO NOTHING
                RETURNING id
                """
            ),
            {
                "source_id": source_id,
                "artifact_id": artifact_id,
                "version_label": version_label,
                "effective_from": effective_from,
                "promulgated_at": item.promulgation_date or body.promulgation_date,
                "raw_manifest_json": json.dumps(manifest, ensure_ascii=False),
            },
        )
        row = inserted.first()
        if row is None:
            existing = await self.session.execute(
                text(
                    """
                    SELECT sv.id, a.id AS artifact_id, a.content_hash
                    FROM source_version sv
                    JOIN artifact a ON a.id = sv.artifact_id
                    WHERE sv.source_id = :source_id AND sv.version_label = :version_label
                    """
                ),
                {"source_id": source_id, "version_label": version_label},
            )
            existing_row = existing.one()
            if existing_row.content_hash != body_artifact.content_hash:
                await self.session.execute(
                    text(
                        """
                        INSERT INTO quality_flag(
                            target_table, target_id, flag_type, severity, message
                        )
                        VALUES (
                            'source_version', :target_id, 'conflict', 'high', :message
                        )
                        """
                    ),
                    {
                        "target_id": existing_row.id,
                        "message": (
                            "Same official version label returned different bytes; "
                            "canonical version was not overwritten"
                        ),
                    },
                )
                return PersistedLawVersion(
                    source_id=source_id,
                    source_version_id=existing_row.id,
                    artifact_id=existing_row.artifact_id,
                    created=False,
                    conflict=True,
                )
            return PersistedLawVersion(
                source_id=source_id,
                source_version_id=existing_row.id,
                artifact_id=existing_row.artifact_id,
                created=False,
            )

        source_version_id: UUID = row[0]
        event_payload = {
            "source_id": str(source_id),
            "source_version_id": str(source_version_id),
            "artifact_id": str(artifact_id),
            "source_key": source_key,
            "version_label": version_label,
        }
        event_result = await self.session.execute(
            text(
                """
                INSERT INTO domain_event(
                    aggregate_type, aggregate_id, event_type, payload_json
                )
                VALUES (
                    'source_version', :aggregate_id, 'SourceVersionIngested',
                    CAST(:payload_json AS jsonb)
                )
                RETURNING event_id
                """
            ),
            {
                "aggregate_id": str(source_version_id),
                "payload_json": json.dumps(event_payload),
            },
        )
        event_id = event_result.scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (
                    :event_id, 'normalization.source-version', CAST(:payload_json AS jsonb)
                )
                """
            ),
            {"event_id": event_id, "payload_json": json.dumps(event_payload)},
        )
        return PersistedLawVersion(
            source_id=source_id,
            source_version_id=source_version_id,
            artifact_id=artifact_id,
            created=True,
        )
