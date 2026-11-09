"""Verify a restored (or freshly backed-up) deployment: artifacts vs DB records, migrations.

Usage::

    ARCHONTOS_DATABASE_URL=... python -m archontos.ops.verify_restore
    python -m archontos.ops.verify_restore --dsn "$SERVICE_DSN" --artifact-backend local \
        --artifact-path /restore/artifacts [--skip-artifacts] [--json]

Checks (read-only, exit 1 when any finding is reported, 2 on usage/connection errors):

* ``schema_migrations`` vs the ``db/migrations`` files: missing, edited (checksum) and unknown
  versions are findings; pending versions are reported as findings too (a restore must land on
  the version the code expects, or you must run ``python -m archontos.db.migrate`` first).
* every ``artifact`` row: the object at ``storage_uri`` exists, its sha256 equals
  ``content_hash`` and its length equals ``byte_size`` (when recorded).
* ``audit_log`` is still append-only (forced RLS, no UPDATE/DELETE/TRUNCATE for this login).

The DSN and storage secrets are read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import asyncpg

from archontos.db import migrate


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    storage_uri: str
    content_hash: str
    byte_size: int | None


class ArtifactReader(Protocol):
    def read_bytes(self, storage_uri: str) -> bytes | None:
        """Return the object bytes, or None when it does not exist."""
        ...


class LocalArtifactReader:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def read_bytes(self, storage_uri: str) -> bytes | None:
        if not storage_uri.startswith("local://"):
            raise ValueError("not a local:// URI")
        path = (self.root / storage_uri.removeprefix("local://")).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes the local root")
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None


class MinioArtifactReader:
    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool
    ):
        from minio import Minio

        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket

    def read_bytes(self, storage_uri: str) -> bytes | None:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            raise ValueError("URI is outside the configured MinIO bucket")
        try:
            response = self.client.get_object(self.bucket, parsed.path.lstrip("/"))
        except Exception as exc:  # noqa: BLE001 - minio raises S3Error; only "missing" is expected
            if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return None
            raise
        try:
            return bytes(response.read())
        finally:
            response.close()
            response.release_conn()


def verify_artifacts(rows: Sequence[ArtifactRow], reader: ArtifactReader) -> list[Finding]:
    findings: list[Finding] = []
    for row in rows:
        try:
            data = reader.read_bytes(row.storage_uri)
        except ValueError as exc:
            findings.append(Finding("artifact", row.storage_uri, f"unreadable: {exc}"))
            continue
        if data is None:
            findings.append(Finding("artifact", row.storage_uri, "object missing from store"))
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != row.content_hash:
            findings.append(
                Finding(
                    "artifact",
                    row.storage_uri,
                    f"sha256 mismatch: db={row.content_hash} actual={digest}",
                )
            )
        if row.byte_size is not None and row.byte_size != len(data):
            findings.append(
                Finding(
                    "artifact",
                    row.storage_uri,
                    f"size mismatch: db={row.byte_size} actual={len(data)}",
                )
            )
    return findings


def verify_migrations(
    applied: Mapping[str, str], available: Mapping[str, str], *, allow_pending: bool = False
) -> list[Finding]:
    """``applied``/``available`` map migration version to checksum."""
    findings: list[Finding] = []
    for version, checksum in sorted(available.items()):
        if version not in applied:
            if not allow_pending:
                findings.append(
                    Finding("migrations", version, "pending (not in schema_migrations)")
                )
        elif applied[version] != checksum:
            findings.append(Finding("migrations", version, "checksum differs from the file"))
    for version in sorted(set(applied) - set(available)):
        findings.append(Finding("migrations", version, "applied but no such migration file"))
    return findings


def verify_audit_log(
    *, force_rls: bool, is_superuser: bool, can_update: bool, can_delete: bool, can_truncate: bool
) -> list[Finding]:
    findings: list[Finding] = []
    if not force_rls:
        findings.append(Finding("audit_log", "audit_log", "FORCE ROW LEVEL SECURITY is off"))
    if not is_superuser:
        for name, allowed in (
            ("UPDATE", can_update),
            ("DELETE", can_delete),
            ("TRUNCATE", can_truncate),
        ):
            if allowed:
                findings.append(
                    Finding("audit_log", "audit_log", f"this login holds {name} on audit_log")
                )
    return findings


async def load_artifact_rows(conn: asyncpg.Connection) -> list[ArtifactRow]:
    rows = await conn.fetch("SELECT storage_uri, content_hash, byte_size FROM artifact ORDER BY id")
    return [ArtifactRow(r["storage_uri"], r["content_hash"], r["byte_size"]) for r in rows]


async def load_applied_migrations(conn: asyncpg.Connection) -> dict[str, str]:
    if not await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL"):
        return {}
    return {
        r["version"]: r["checksum"]
        for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")
    }


async def load_audit_log_state(conn: asyncpg.Connection) -> dict[str, bool]:
    row = await conn.fetchrow(
        """
        SELECT c.relforcerowsecurity AS force_rls,
               (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,
               has_table_privilege(c.oid, 'UPDATE') AS can_update,
               has_table_privilege(c.oid, 'DELETE') AS can_delete,
               has_table_privilege(c.oid, 'TRUNCATE') AS can_truncate
        FROM pg_class c
        WHERE c.oid = to_regclass('audit_log')
        """
    )
    if row is None:
        raise RuntimeError("audit_log table not found")
    return {k: bool(v) for k, v in dict(row).items()}


async def run_checks(
    conn: asyncpg.Connection,
    reader: ArtifactReader | None,
    *,
    migrations_dir: Path | None = None,
    allow_pending: bool = False,
) -> list[Finding]:
    available = {m.version: m.checksum for m in migrate.discover(migrations_dir)}
    findings = verify_migrations(
        await load_applied_migrations(conn), available, allow_pending=allow_pending
    )
    findings += verify_audit_log(**await load_audit_log_state(conn))
    if reader is not None:
        rows = await load_artifact_rows(conn)
        # Hashing is blocking file/network I/O: keep it off the event loop.
        findings += await asyncio.to_thread(verify_artifacts, rows, reader)
    return findings


def build_reader(args: argparse.Namespace) -> ArtifactReader:
    if args.artifact_backend == "local":
        return LocalArtifactReader(args.artifact_path)
    from archontos.config import get_settings

    s = get_settings()
    return MinioArtifactReader(
        endpoint=s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        bucket=s.minio_bucket,
        secure=s.minio_secure,
    )


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archontos.ops.verify_restore")
    parser.add_argument("--dsn", default=None, help="default: ARCHONTOS_DATABASE_URL")
    parser.add_argument(
        "--dir", default=None, type=Path, help="migrations dir; default db/migrations"
    )
    parser.add_argument(
        "--artifact-backend",
        choices=("local", "minio"),
        default=os.getenv("ARCHONTOS_ARTIFACT_BACKEND", "local"),
    )
    parser.add_argument(
        "--artifact-path",
        default=os.getenv("ARCHONTOS_ARTIFACT_LOCAL_PATH", "local-data/artifacts"),
    )
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument(
        "--allow-pending", action="store_true", help="do not flag pending migrations"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    dsn = args.dsn or os.getenv("ARCHONTOS_DATABASE_URL")
    if not dsn:
        print("no DSN: pass --dsn or set ARCHONTOS_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        conn = await asyncpg.connect(migrate.plain_dsn(dsn), timeout=30)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"cannot connect to the database: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        reader = None if args.skip_artifacts else build_reader(args)
        findings = await run_checks(
            conn, reader, migrations_dir=args.dir, allow_pending=args.allow_pending
        )
    finally:
        await conn.close()
    report: dict[str, Any] = {"ok": not findings, "findings": [asdict(f) for f in findings]}
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False))
    elif findings:
        for f in findings:
            print(f"FAIL [{f.check}] {f.subject}: {f.message}", file=sys.stderr)
    else:
        print("restore verification OK")
    return 1 if findings else 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":  # pragma: no cover
    main()
