from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.ingestion.adapters import LawGoKrAdapter, LawSearchItem
from archontos.ingestion.persistence import (
    CanonicalLawRepository,
    PersistedLawVersion,
    SourceVersionConflict,
)
from archontos.storage.artifacts import ArtifactStore


class LawNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class LawIngestionResult:
    item: LawSearchItem
    persisted: PersistedLawVersion
    body_sha256: str
    discovery_sha256: str


class LawIngestionService:
    def __init__(
        self,
        *,
        adapter: LawGoKrAdapter,
        artifact_store: ArtifactStore,
        session_factory: async_sessionmaker,
    ):
        self.adapter = adapter
        self.artifact_store = artifact_store
        self.session_factory = session_factory

    async def ingest_current(self, query: str) -> LawIngestionResult:
        page = await self.adapter.search_laws(query, display=100)
        exact = [item for item in page.items if item.law_name.strip() == query.strip()]
        if len(exact) != 1:
            raise LawNotFoundError(
                f"expected one exact official-law match for {query!r}; found {len(exact)}"
            )
        item = exact[0]
        body = await self.adapter.fetch_law(law_id=item.law_id or None, mst=item.mst or None)

        discovery_artifact = await self.artifact_store.put_envelope(page.envelope)
        body_artifact = await self.artifact_store.put_envelope(body.envelope)

        async with self.session_factory() as session:
            async with session.begin():
                persisted = await CanonicalLawRepository(session).persist_law_version(
                    item=item,
                    body=body,
                    body_artifact=body_artifact,
                    discovery_artifact=discovery_artifact,
                )
        if persisted.conflict:
            raise SourceVersionConflict(
                f"official version {item.law_id}/{item.mst} changed bytes; canonical kept"
            )
        return LawIngestionResult(
            item=item,
            persisted=persisted,
            body_sha256=body_artifact.content_hash,
            discovery_sha256=discovery_artifact.content_hash,
        )
