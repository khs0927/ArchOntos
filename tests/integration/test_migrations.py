"""Migration runner: fresh, idempotent, upgrade == fresh, edited-file and initdb guards."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from archontos.db.migrate import MigrationError, apply_migrations, discover, status

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


@pytest.fixture(scope="module", autouse=True)
def _extensions_in_public(postgres_dsn):
    """These tests keep two throwaway schemas alive at once. Migration 001's CREATE EXTENSION would
    put pgvector into whichever schema runs first, and the second schema could not see the type, so
    install the extensions in ``public`` (on every test search_path) up front."""

    async def install():
        conn = await asyncpg.connect(postgres_dsn)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        finally:
            await conn.close()

    asyncio.run(install())


async def _schema_conn(dsn: str):
    schema = f"archontos_mig_{uuid4().hex}"
    conn = await asyncpg.connect(dsn)
    # Fail instead of hanging forever if a broken test leaves a lock behind.
    await conn.execute("SET lock_timeout = '30s'")
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    return conn, schema


async def _drop(conn, schema: str) -> None:
    await conn.execute("SET search_path TO public")
    await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await conn.close()


async def _shape(conn, schema: str) -> list[tuple]:
    columns = await conn.fetch(
        """SELECT table_name, column_name, data_type, is_nullable, column_default
             FROM information_schema.columns WHERE table_schema = $1
            ORDER BY table_name, column_name""",
        schema,
    )
    indexes = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1 ORDER BY indexname",
        schema,
    )
    constraints = await conn.fetch(
        """SELECT conrelid::regclass::text AS rel, conname, pg_get_constraintdef(oid) AS def
             FROM pg_constraint WHERE connamespace = $1::regnamespace ORDER BY rel, conname""",
        schema,
    )

    def strip(value):
        return value.replace(schema, "S") if isinstance(value, str) else value

    return [tuple(strip(v) for v in r.values()) for r in [*columns, *indexes, *constraints]]


@pytest.mark.asyncio
async def test_fresh_apply_is_recorded_and_idempotent(postgres_dsn):
    conn, schema = await _schema_conn(postgres_dsn)
    try:
        applied = await apply_migrations(conn, MIGRATIONS_DIR)
        assert applied == [m.version for m in discover(MIGRATIONS_DIR)]
        assert await apply_migrations(conn, MIGRATIONS_DIR) == []
        state = await status(conn, MIGRATIONS_DIR)
        assert state["pending"] == [] and state["edited"] == []
    finally:
        await _drop(conn, schema)


@pytest.mark.asyncio
async def test_upgrade_reaches_the_same_schema_as_fresh(postgres_dsn, tmp_path):
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    old = tmp_path / "old"
    old.mkdir()
    for f in files[:3]:  # a database deployed before the later migrations existed
        shutil.copy(f, old / f.name)
    fresh, fresh_schema = await _schema_conn(postgres_dsn)
    upgraded, upgraded_schema = await _schema_conn(postgres_dsn)
    try:
        await apply_migrations(fresh, MIGRATIONS_DIR)
        await apply_migrations(upgraded, old)
        later = await apply_migrations(upgraded, MIGRATIONS_DIR)
        assert later == [f.stem for f in files[3:]]
        assert await _shape(upgraded, upgraded_schema) == await _shape(fresh, fresh_schema)
    finally:
        await _drop(fresh, fresh_schema)
        await _drop(upgraded, upgraded_schema)


@pytest.mark.asyncio
async def test_edited_applied_migration_is_refused(postgres_dsn, tmp_path):
    edited = tmp_path / "m"
    shutil.copytree(MIGRATIONS_DIR, edited)
    conn, schema = await _schema_conn(postgres_dsn)
    try:
        await apply_migrations(conn, edited)
        last = sorted(edited.glob("*.sql"))[-1]
        last.write_text(last.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        with pytest.raises(MigrationError, match="checksum"):
            await apply_migrations(conn, edited)
    finally:
        await _drop(conn, schema)


@pytest.mark.asyncio
async def test_crlf_checkout_is_not_an_edit(postgres_dsn, tmp_path):
    crlf = tmp_path / "crlf"
    crlf.mkdir()
    conn, schema = await _schema_conn(postgres_dsn)
    try:
        await apply_migrations(conn, MIGRATIONS_DIR)
        for f in MIGRATIONS_DIR.glob("*.sql"):
            (crlf / f.name).write_bytes(
                f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            )
        assert await apply_migrations(conn, crlf) == []
    finally:
        await _drop(conn, schema)


@pytest.mark.asyncio
async def test_initdb_database_needs_an_explicit_baseline(postgres_dsn):
    conn, schema = await _schema_conn(postgres_dsn)
    try:
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):  # what docker-entrypoint-initdb.d used to do
            await conn.execute(f.read_text(encoding="utf-8"))
        with pytest.raises(MigrationError, match="--baseline"):
            await apply_migrations(conn, MIGRATIONS_DIR)
        last = discover(MIGRATIONS_DIR)[-1].version
        assert await apply_migrations(conn, MIGRATIONS_DIR, baseline=last.split("_", 1)[0]) == []
        assert (await status(conn, MIGRATIONS_DIR))["pending"] == []
    finally:
        await _drop(conn, schema)


def test_duplicate_numbers_are_rejected(tmp_path: Path):
    (tmp_path / "004_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "004_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate"):
        discover(tmp_path)
