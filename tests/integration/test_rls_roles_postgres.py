"""Least-privilege service role and jurisdiction RLS, exercised as the non-superuser login."""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from archontos.db.roles import inspect_current_role
from archontos.identity import Principal, reset_principal, set_principal

pytestmark = pytest.mark.asyncio

INSERT_DOC = """
    INSERT INTO source_document(
        source_key, title, issuer, jurisdiction_code, document_type
    )
    VALUES (:key, 't', 'i', 'KR', 'statute')
"""


async def _count_docs(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(text("SELECT count(*) FROM source_document"))).scalar_one()


def _as(jurisdictions):
    return set_principal(Principal("tester", True, jurisdictions=jurisdictions))


async def test_service_login_is_unprivileged(mvp0_db):
    session_factory, _ = mvp0_db
    async with session_factory() as session:
        conn = await session.connection()
        raw = await conn.get_raw_connection()
        privileges = await inspect_current_role(raw.driver_connection)
    assert privileges.problems == []


async def test_admin_role_is_reported_as_privileged(postgres_dsn):
    admin = await asyncpg.connect(postgres_dsn)
    try:
        privileges = await inspect_current_role(admin)
    finally:
        await admin.close()
    assert any("superuser" in problem for problem in privileges.problems)


async def test_jurisdiction_rls_scopes_reads_and_writes(mvp0_db):
    session_factory, _ = mvp0_db
    async with session_factory() as session, session.begin():  # default: KR
        await session.execute(text(INSERT_DOC), {"key": "doc:kr"})
    assert await _count_docs(session_factory) == 1

    token = _as(("JP",))
    try:
        assert await _count_docs(session_factory) == 0
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session_factory() as session, session.begin():
                await session.execute(text(INSERT_DOC), {"key": "doc:kr-2"})
        async with session_factory() as session, session.begin():
            updated = await session.execute(text("UPDATE source_document SET title = 'x'"))
        assert updated.rowcount == 0
    finally:
        reset_principal(token)

    token = _as(())
    try:
        assert await _count_docs(session_factory) == 0  # fail-closed on an empty grant
    finally:
        reset_principal(token)

    token = _as(("JP", "KR"))
    try:
        assert await _count_docs(session_factory) == 1
    finally:
        reset_principal(token)


async def test_owner_is_not_subject_to_jurisdiction_rls(mvp0_db, postgres_dsn):
    session_factory, schema = mvp0_db
    async with session_factory() as session, session.begin():
        await session.execute(text(INSERT_DOC), {"key": "doc:kr"})
    admin = await asyncpg.connect(postgres_dsn)
    try:
        # No app.allowed_jurisdictions at all: the migrator still sees everything.
        assert await admin.fetchval(f'SELECT count(*) FROM "{schema}".source_document') == 1
    finally:
        await admin.close()


async def test_startup_check_enforce_refuses_superuser(postgres_dsn):
    from archontos.db.roles import verify_service_login

    with pytest.raises(RuntimeError, match="bypass row-level security"):
        await verify_service_login(postgres_dsn, "enforce")
    assert await verify_service_login(postgres_dsn, "warn")  # problems reported, no raise
    assert await verify_service_login(postgres_dsn, "off") == []
