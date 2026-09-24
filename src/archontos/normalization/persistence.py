from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.normalization.legal import LegalEvidenceUnit


class EvidencePersistenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvidencePersistenceResult:
    source_version_id: UUID
    evidence_ids: tuple[UUID, ...]

    @property
    def count(self) -> int:
        return len(self.evidence_ids)


class CanonicalEvidenceRepository:
    """Idempotent writes of normalized evidence into the canonical PostgreSQL store."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def persist(
        self,
        *,
        source_version_id: UUID,
        units: tuple[LegalEvidenceUnit, ...] | list[LegalEvidenceUnit],
    ) -> EvidencePersistenceResult:
        artifact_result = await self.session.execute(
            text("SELECT artifact_id FROM source_version WHERE id = :source_version_id"),
            {"source_version_id": source_version_id},
        )
        row = artifact_result.first()
        if row is None:
            raise EvidencePersistenceError(f"unknown source_version_id: {source_version_id}")
        artifact_id = row[0]

        evidence_ids: list[UUID] = []
        for unit in units:
            result = await self.session.execute(
                text(
                    """
                    INSERT INTO evidence_span(
                        source_version_id, artifact_id, evidence_key, locator_json,
                        text_snippet, normalized_text_hash, extractor_method,
                        extraction_confidence
                    )
                    VALUES (
                        :source_version_id, :artifact_id, :evidence_key,
                        CAST(:locator_json AS jsonb), :text_snippet, :normalized_text_hash,
                        :extractor_method, :extraction_confidence
                    )
                    ON CONFLICT (source_version_id, evidence_key)
                    WHERE evidence_key IS NOT NULL
                    DO UPDATE SET
                        artifact_id = EXCLUDED.artifact_id,
                        locator_json = EXCLUDED.locator_json,
                        text_snippet = EXCLUDED.text_snippet,
                        normalized_text_hash = EXCLUDED.normalized_text_hash,
                        extractor_method = EXCLUDED.extractor_method,
                        extraction_confidence = EXCLUDED.extraction_confidence
                    RETURNING id
                    """
                ),
                {
                    "source_version_id": source_version_id,
                    "artifact_id": artifact_id,
                    "evidence_key": unit.evidence_key,
                    "locator_json": json.dumps(unit.locator, ensure_ascii=False, sort_keys=True),
                    "text_snippet": unit.text_snippet,
                    "normalized_text_hash": unit.normalized_text_hash,
                    "extractor_method": unit.extractor_method,
                    "extraction_confidence": unit.extraction_confidence,
                },
            )
            evidence_ids.append(result.scalar_one())

        return EvidencePersistenceResult(
            source_version_id=source_version_id,
            evidence_ids=tuple(evidence_ids),
        )
