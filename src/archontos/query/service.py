from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.query.persistence import CanonicalQueryRepository


class CanonicalQueryService:
    def __init__(self, *, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def source_evidence(self, rule_version_id: UUID):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).source_evidence(rule_version_id)

    async def decision_provenance(self, decision_id: UUID):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).decision_provenance(decision_id)

    async def authority(self, rule_version_id: UUID):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).authority(rule_version_id)

    async def applicability(self, rule_version_id: UUID):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).applicability(rule_version_id)

    async def temporal_comparison(
        self,
        *,
        source_key: str,
        left_date: date,
        right_date: date,
    ):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).temporal_comparison(
                source_key=source_key,
                left_date=left_date,
                right_date=right_date,
            )

    async def jurisdiction_comparison(
        self,
        *,
        rule_title: str,
        left_jurisdiction: str,
        right_jurisdiction: str,
        at_date: date,
    ):
        async with self.session_factory() as session:
            return await CanonicalQueryRepository(session).jurisdiction_comparison(
                rule_title=rule_title,
                left_jurisdiction=left_jurisdiction,
                right_jurisdiction=right_jurisdiction,
                at_date=at_date,
            )
