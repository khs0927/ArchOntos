"""Rebuild the HNSW index with chosen build parameters, without blocking writes.

Migration 010 creates ``hnsw_embedding_projection_cosine`` with pgvector defaults
(m=16, ef_construction=64). Higher values give better recall at the cost of build time and
memory. Build parameters are a property of the index, so changing them means a rebuild::

    python -m archontos.db.vector_index --m 24 --ef-construction 128 [--dsn ...]
    python -m archontos.db.vector_index --show

The new index is built ``CONCURRENTLY`` under a temporary name, then swapped in, so reads
and writes continue during the build. Run it as the migrator (index owner).
Query-time tuning is separate: ARCHONTOS_HNSW_EF_SEARCH / _ITERATIVE_SCAN / _MAX_SCAN_TUPLES.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from archontos.db.migrate import plain_dsn

INDEX_NAME = "hnsw_embedding_projection_cosine"
_TMP_NAME = INDEX_NAME + "_rebuild"


async def show(conn: asyncpg.Connection) -> dict[str, str]:
    options = await conn.fetchval(
        """
        SELECT c.reloptions FROM pg_class c
        WHERE c.relname = $1 AND c.relnamespace = to_regnamespace(current_schema())::oid
        """,
        INDEX_NAME,
    )
    if options is None:
        exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", INDEX_NAME)
        return {} if exists else {"error": "index not found"}
    return dict(item.split("=", 1) for item in options)


async def rebuild(conn: asyncpg.Connection, *, m: int, ef_construction: int) -> dict[str, str]:
    if not 2 <= m <= 100:
        raise ValueError("m must be in [2, 100]")
    if not 4 <= ef_construction <= 1000 or ef_construction < 2 * m:
        raise ValueError("ef_construction must be in [4, 1000] and >= 2 * m")
    # CONCURRENTLY cannot run inside a transaction block; asyncpg runs this in autocommit.
    await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_TMP_NAME}")
    await conn.execute(
        f"CREATE INDEX CONCURRENTLY {_TMP_NAME} ON embedding_projection "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)}) "
        f"WHERE embedding IS NOT NULL"
    )
    async with conn.transaction():
        await conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
        await conn.execute(f"ALTER INDEX {_TMP_NAME} RENAME TO {INDEX_NAME}")
    return await show(conn)


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archontos.db.vector_index")
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=64)
    parser.add_argument("--show", action="store_true", help="print current build parameters")
    parser.add_argument("--dsn", default=None, help="migrator DSN; default ARCHONTOS_DATABASE_URL")
    args = parser.parse_args(argv)
    dsn = args.dsn or os.getenv("ARCHONTOS_DATABASE_URL")
    if not dsn:
        print("no DSN: pass --dsn or set ARCHONTOS_DATABASE_URL", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(plain_dsn(dsn))
    try:
        if args.show:
            print(await show(conn))
            return 0
        print(await rebuild(conn, m=args.m, ef_construction=args.ef_construction))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main()))
