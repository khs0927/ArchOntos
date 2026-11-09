"""``python -m archontos.db.migrate --schema`` migrates into a dedicated schema, idempotently."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import asyncpg

from archontos.db.migrate import _main, discover


def test_cli_schema_option_creates_and_migrates_schema(postgres_dsn: str) -> None:
    schema = f"archontos_cli_{uuid4().hex[:12]}"

    async def run() -> None:
        admin = await asyncpg.connect(postgres_dsn)
        try:
            await admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            await admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
            assert await _main(["--dsn", postgres_dsn, "--schema", schema]) == 0
            assert await _main(["--dsn", postgres_dsn, "--schema", schema]) == 0  # idempotent
            tables = await admin.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = $1", schema
            )
            recorded = await admin.fetchval(f'SELECT count(*) FROM "{schema}".schema_migrations')
            assert tables > 20
            assert recorded == len(discover())
            in_public = await admin.fetchval("SELECT to_regclass('public.outbox_message')")
            assert in_public is None or schema == "public"
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()

    asyncio.run(run())
