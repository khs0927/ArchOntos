"""verify_restore against a real migrated schema, as the non-superuser service login."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from archontos.db.roles import ensure_login
from archontos.ops.verify_restore import LocalArtifactReader, run_checks
from tests.integration.conftest import MIGRATIONS_DIR, _with_credentials


@pytest.mark.asyncio
async def test_run_checks_detects_tampering(mvp0_db, postgres_dsn, tmp_path: Path) -> None:
    _, schema = mvp0_db
    admin = await asyncpg.connect(postgres_dsn)
    conn = None
    login = f"archontos_vr_{uuid4().hex[:12]}"
    try:
        await admin.execute(f'SET search_path TO "{schema}", public')
        payload = b'{"a": 1}'
        (tmp_path / "raw").mkdir()
        (tmp_path / "raw" / "x.json").write_bytes(payload)
        await admin.execute(
            "INSERT INTO artifact(artifact_type, mime, storage_uri, content_hash, byte_size) "
            "VALUES ('json','application/json','local://raw/x.json',$1,$2)",
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )
        password = uuid4().hex
        await ensure_login(admin, login, password)
        conn = await asyncpg.connect(_with_credentials(postgres_dsn, login, password))
        await conn.execute(f'SET search_path TO "{schema}", public')

        reader = LocalArtifactReader(tmp_path)
        assert await run_checks(conn, reader, migrations_dir=MIGRATIONS_DIR) == []

        (tmp_path / "raw" / "x.json").write_bytes(b"tampered-content")
        findings = await run_checks(conn, reader, migrations_dir=MIGRATIONS_DIR)
        assert [f.check for f in findings] == ["artifact", "artifact"]  # hash and size
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute("SET search_path TO public")
        await admin.execute(f'DROP OWNED BY "{login}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{login}"')
        await admin.close()
