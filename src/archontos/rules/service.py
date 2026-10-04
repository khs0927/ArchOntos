from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.rules.persistence import (
    CanonicalRuleCompilerRepository,
    PersistedCompiledRule,
)


class RuleCompilationService:
    def __init__(self, *, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def compile_assertion(self, assertion_id: UUID) -> PersistedCompiledRule:
        async with self.session_factory() as session:
            async with session.begin():
                return await CanonicalRuleCompilerRepository(session).compile_approved_assertion(
                    assertion_id
                )
