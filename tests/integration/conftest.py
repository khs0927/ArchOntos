"""Shared PostgreSQL harness for the MVP-0 integration suite.

The golden-path test previously inlined its own schema create/migrate/drop
sequence. That made a second database-backed test impossible without
copy-pasting ~40 lines, so the lifecycle lives here instead.

Every test runs against a throwaway schema inside a real PostgreSQL instance
and drops it afterwards, so tests never observe each other's rows.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from archontos.db.migrate import apply_migrations
from archontos.ingestion.adapters import LawGoKrAdapter, LawSearchItem, RawSourceEnvelope
from archontos.ingestion.persistence import CanonicalLawRepository

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def test_dsn() -> str | None:
    """Return a plain postgres DSN, or None when no test database is configured."""
    value = os.getenv("ARCHONTOS_TEST_DATABASE_URL")
    if not value:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def sqlalchemy_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = test_dsn()
    if not dsn:
        pytest.skip("ARCHONTOS_TEST_DATABASE_URL is not configured")
    return dsn


@pytest_asyncio.fixture
async def mvp0_db(
    postgres_dsn: str,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """Yield a session factory bound to a fresh migrated schema, then drop it.

    Every table the migrations create lands in the throwaway schema, and the
    suite reads and writes only those. ``public`` stays on the search path
    because migration 001 creates pgvector once and then declares an
    ``embedding vector(1536)`` column, whose type only resolves if the
    extension's schema is reachable. The consequence of that is stated rather
    than glossed over: an unqualified statement naming a table the migrations
    did not create would silently resolve in ``public`` instead of failing.
    ``_assert_schema_is_populated`` closes that hole by failing the fixture if
    any expected table is missing.
    """
    schema = f"archontos_test_{uuid4().hex}"
    admin = await asyncpg.connect(postgres_dsn)
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}", public')
        # The same runner compose's `migrate` service uses (schema_migrations + advisory lock).
        await apply_migrations(admin, MIGRATIONS_DIR)

        await _assert_schema_is_populated(admin, schema)

        engine = create_async_engine(
            sqlalchemy_url(postgres_dsn),
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        yield session_factory, schema
    finally:
        if engine is not None:
            await engine.dispose()
        try:
            await admin.execute("SET search_path TO public")
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()


EXPECTED_TABLES = (
    "source_document",
    "source_version",
    "artifact",
    "evidence_span",
    "assertion",
    "assertion_review",
    "rule",
    "rule_version",
    "rule_assertion",
    "evaluation",
    "decision",
    "outbox_message",
    "domain_event",
    "quality_flag",
)


async def _assert_schema_is_populated(admin, schema: str) -> None:
    """Fail loudly if a migration did not produce every table the suite needs.

    Without this, a migration that silently no-ops leaves the schema partial
    and the next unqualified statement falls through to ``public``, which would
    both pass the test and touch the shared database.
    """
    missing = []
    for table in EXPECTED_TABLES:
        found = await admin.fetchval("SELECT to_regclass($1)", f"{schema}.{table}")
        if found is None:
            missing.append(table)
    if missing:
        raise AssertionError(
            f"migrations did not create {missing} in the throwaway schema; "
            "refusing to run against the shared public schema"
        )


# --- shared fixture builders -------------------------------------------------
# Kept here so the golden-path test and the idempotency test cannot drift apart.


def body_payload(*, mst: str, enforcement_date: str, required_count: int) -> dict:
    return {
        "법령": {
            "법령키": mst,
            "기본정보": {
                "법령명_한글": "테스트 건축법",
                "법령ID": "001823",
                "법령일련번호": mst,
                "법종구분": {"content": "법률"},
                "소관부처": {"content": "국토교통부"},
                "공포일자": "20260101",
                "시행일자": enforcement_date,
            },
            "조문": {
                "조문단위": {
                    "조문번호": "10",
                    "조문제목": "직통계단",
                    "조문내용": "직통계단 설치기준",
                    "항": {
                        "항번호": "1",
                        "항내용": "직통계단 수에 관한 기준",
                        "호": {
                            "호번호": "1",
                            "호내용": f"직통계단을 {required_count}개소 이상 설치한다.",
                        },
                    },
                }
            },
            "부칙": {"부칙단위": {"부칙공포일자": "20260101", "부칙내용": "부칙"}},
            "별표": {
                "별표단위": {
                    "별표번호": "1",
                    "별표제목": "테스트 별표",
                    "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=123",
                }
            },
        }
    }


def law_item(*, mst: str, enforcement_date: date) -> LawSearchItem:
    return LawSearchItem(
        law_name="테스트 건축법",
        law_id="001823",
        mst=mst,
        law_type="법률",
        ministry="국토교통부",
        promulgation_date=date(2026, 1, 1),
        promulgation_number="21000",
        enforcement_date=enforcement_date,
        revision_type="일부개정",
        history_code="현행",
        detail_link=f"/DRF/lawService.do?target=law&MST={mst}",
    )


async def persist_law_version(
    *,
    session_factory,
    store,
    mst: str,
    enforcement_date: date,
    required_count: int,
):
    """Fetch-free equivalent of the ingestion path: envelope -> artifact -> canonical upsert."""
    envelope = RawSourceEnvelope(
        source_name="law.go.kr",
        endpoint="https://www.law.go.kr/DRF/lawService.do",
        params={"target": "law", "MST": mst, "ID": "001823"},
        payload=body_payload(
            mst=mst,
            enforcement_date=enforcement_date.strftime("%Y%m%d"),
            required_count=required_count,
        ),
        fetched_at=datetime.now(UTC),
    )
    artifact = await store.put_envelope(envelope)
    body = LawGoKrAdapter.parse_body(envelope)
    item = law_item(mst=mst, enforcement_date=enforcement_date)

    async with session_factory() as session:
        async with session.begin():
            return await CanonicalLawRepository(session).persist_law_version(
                item=item,
                body=body,
                body_artifact=artifact,
            )


async def count_rows(session_factory, table: str, where: str = "TRUE") -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))
        return result.scalar_one()
