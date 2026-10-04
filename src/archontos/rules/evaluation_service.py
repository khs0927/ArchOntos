from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.rules.evaluation import CanonicalEvaluationRepository, PersistedEvaluation


class CanonicalEvaluationService:
    def __init__(self, *, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def evaluate(
        self,
        *,
        rule_version_id: UUID,
        facts: dict[str, Any],
        object_version_id: UUID | None = None,
        evaluated_at: datetime | None = None,
    ) -> PersistedEvaluation:
        async with self.session_factory() as session:
            async with session.begin():
                return await CanonicalEvaluationRepository(session).evaluate(
                    rule_version_id=rule_version_id,
                    facts=facts,
                    object_version_id=object_version_id,
                    evaluated_at=evaluated_at,
                )
