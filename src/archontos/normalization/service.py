from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.normalization.legal import LawEvidenceNormalizer
from archontos.normalization.persistence import CanonicalEvidenceRepository


@dataclass(frozen=True, slots=True)
class LawNormalizationResult:
    source_version_id: UUID
    evidence_count: int


class LawNormalizationService:
    def __init__(self, *, session_factory: async_sessionmaker):
        self.session_factory = session_factory
        self.normalizer = LawEvidenceNormalizer()

    async def normalize_body(
        self, *, source_version_id: UUID, body: object
    ) -> LawNormalizationResult:
        units = self.normalizer.normalize(body)
        async with self.session_factory() as session:
            async with session.begin():
                persisted = await CanonicalEvidenceRepository(session).persist(
                    source_version_id=source_version_id,
                    units=units,
                )
        return LawNormalizationResult(
            source_version_id=source_version_id,
            evidence_count=persisted.count,
        )
