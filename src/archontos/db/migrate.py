"""Versioned SQL migration runner: the one path for CI, tests, compose and upgrades.

``db/migrations/NNN_name.sql`` files are applied in name order, each exactly once, and recorded
in ``schema_migrations`` (version, sha256 checksum, applied_at). A Postgres advisory lock lets
several services start at once: the second runner waits, then finds everything recorded.

Before this runner the only production path was ``docker-entrypoint-initdb.d``, which runs on an
EMPTY volume only, so a deployed database never received a new migration.

Usage::

    python -m archontos.db.migrate                 # apply pending migrations
    python -m archontos.db.migrate --status        # list applied / pending, change nothing
    python -m archontos.db.migrate --baseline 007  # adopt a DB created by the old initdb path

DSN: ``ARCHONTOS_DATABASE_URL`` (``postgresql+asyncpg://`` is accepted) or ``--dsn``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

MIGRATION_LOCK_KEY = 7_146_221_101
_SELF_TRANSACTIONAL = re.compile(r"^\s*BEGIN\s*;", re.IGNORECASE)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8-sig")

    @property
    def checksum(self) -> str:
        # Line endings normalised: a Windows checkout (CRLF) must not look like an edited migration.
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def default_migrations_dir() -> Path:
    configured = os.getenv("ARCHONTOS_MIGRATIONS_DIR")
    if configured:
        return Path(configured)
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        candidate = base / "db" / "migrations"
        if candidate.is_dir():
            return candidate
    raise MigrationError("db/migrations not found; set ARCHONTOS_MIGRATIONS_DIR")


def discover(migrations_dir: Path | None = None) -> list[Migration]:
    folder = Path(migrations_dir) if migrations_dir else default_migrations_dir()
    found = [Migration(p.stem, p) for p in sorted(folder.glob("*.sql"))]
    seen: dict[str, str] = {}
    for m in found:
        prefix = m.version.split("_", 1)[0]
        if prefix in seen:
            raise MigrationError(
                f"duplicate migration number {prefix}: {seen[prefix]} and {m.version}"
            )
        seen[prefix] = m.version
    return found


def plain_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version text PRIMARY KEY,
               checksum text NOT NULL,
               applied_at timestamptz NOT NULL DEFAULT now())"""
    )


async def _applied(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def _schema_has_tables(conn: asyncpg.Connection) -> bool:
    schema = await conn.fetchval("SELECT current_schema()")
    count = await conn.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname = $1 AND tablename <> 'schema_migrations'",
        schema,
    )
    return bool(count)


async def apply_migrations(
    conn: asyncpg.Connection, migrations_dir: Path | None = None, *, baseline: str | None = None
) -> list[str]:
    """Apply pending migrations on ``conn`` (current search_path); return the versions applied.

    ``baseline`` records every migration up to and including that version as applied without
    running it: for databases created by the old initdb mount, which have the schema but no
    ``schema_migrations`` rows.
    """
    migrations = discover(migrations_dir)
    await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
    try:
        fresh_table = await conn.fetchval("SELECT to_regclass('schema_migrations') IS NULL")
        if fresh_table and baseline is None and await _schema_has_tables(conn):
            raise MigrationError(
                "this schema already has tables but no schema_migrations (created by the old "
                "initdb mount). Check which db/migrations files it has, then run "
                "`python -m archontos.db.migrate --baseline <last applied version>` once."
            )
        await _ensure_table(conn)
        applied = await _applied(conn)
        if baseline is not None:
            versions = [m.version for m in migrations]
            match = [v for v in versions if v == baseline or v.split("_", 1)[0] == baseline]
            if not match:
                raise MigrationError(f"unknown baseline {baseline!r}; known: {versions}")
            cutoff = versions.index(match[0])
            for m in migrations[: cutoff + 1]:
                if m.version not in applied:
                    await conn.execute(
                        "INSERT INTO schema_migrations(version, checksum) VALUES ($1, $2)",
                        m.version,
                        m.checksum,
                    )
                    applied[m.version] = m.checksum
        done: list[str] = []
        for m in migrations:
            if m.version in applied:
                if applied[m.version] != m.checksum:
                    raise MigrationError(
                        f"{m.version} was edited after it was applied (checksum mismatch); "
                        "add a new migration instead of changing an applied one"
                    )
                continue
            sql = m.sql
            if _SELF_TRANSACTIONAL.match(sql):
                # The file carries its own BEGIN/COMMIT; record it right after it commits. On error
                # the script leaves its transaction open and aborted: roll it back so the session
                # releases its locks (an aborted, open transaction blocks other sessions' DDL).
                try:
                    await conn.execute(sql)
                except BaseException:
                    if conn.is_in_transaction():
                        await conn.execute("ROLLBACK")
                    raise
                await conn.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES ($1, $2)",
                    m.version,
                    m.checksum,
                )
            else:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations(version, checksum) VALUES ($1, $2)",
                        m.version,
                        m.checksum,
                    )
            done.append(m.version)
        return done
    finally:
        if conn.is_in_transaction():
            await conn.execute("ROLLBACK")
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)


async def status(
    conn: asyncpg.Connection, migrations_dir: Path | None = None
) -> dict[str, list[str]]:
    migrations = discover(migrations_dir)
    exists = await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")
    applied = await _applied(conn) if exists else {}
    return {
        "applied": [m.version for m in migrations if m.version in applied],
        "pending": [m.version for m in migrations if m.version not in applied],
        "edited": [
            m.version
            for m in migrations
            if m.version in applied and applied[m.version] != m.checksum
        ],
    }


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m archontos.db.migrate", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--dsn", default=None, help="default: ARCHONTOS_DATABASE_URL")
    parser.add_argument("--dir", default=None, type=Path, help="default: ./db/migrations")
    parser.add_argument("--status", action="store_true", help="show applied/pending and exit")
    parser.add_argument(
        "--baseline", default=None, help="mark migrations up to VERSION as applied (initdb DBs)"
    )
    args = parser.parse_args(argv)
    dsn = args.dsn or os.getenv("ARCHONTOS_DATABASE_URL")
    if not dsn:
        from archontos.config import get_settings

        dsn = get_settings().database_url
    conn = await asyncpg.connect(plain_dsn(dsn), timeout=30)
    try:
        if args.status:
            print(await status(conn, args.dir))
            return 0
        applied = await apply_migrations(conn, args.dir, baseline=args.baseline)
        print(f"applied {len(applied)} migration(s): {', '.join(applied) or '-'}")
        return 0
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(asyncio.run(_main(argv)))


if __name__ == "__main__":
    main()
