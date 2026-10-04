from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.persistence import (
    CanonicalAssertionRepository,
    PersistedAssertion,
    PersistedAssertionReview,
)
from archontos.domain.enums import ReviewStatus


class AssertionWorkflowService:
    def __init__(self, *, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def create_candidate(
        self,
        payload: AssertionCandidateCreate,
    ) -> PersistedAssertion:
        async with self.session_factory() as session:
            async with session.begin():
                return await CanonicalAssertionRepository(session).create_candidate(payload)

    async def review(
        self,
        *,
        assertion_id: UUID,
        decision: ReviewStatus,
        reviewer_id: str,
        note: str | None = None,
    ) -> PersistedAssertionReview:
        async with self.session_factory() as session:
            async with session.begin():
                return await CanonicalAssertionRepository(session).review(
                    assertion_id=assertion_id,
                    decision=decision,
                    reviewer_id=reviewer_id,
                    note=note,
                )
