"""verify_restore pure checks: no database, fakes only."""

from __future__ import annotations

import hashlib
from pathlib import Path

from archontos.ops.verify_restore import (
    ArtifactRow,
    LocalArtifactReader,
    verify_artifacts,
    verify_audit_log,
    verify_migrations,
)


class FakeReader:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def read_bytes(self, storage_uri: str) -> bytes | None:
        return self.objects.get(storage_uri)


def _row(uri: str, data: bytes, size: int | None = None) -> ArtifactRow:
    return ArtifactRow(uri, hashlib.sha256(data).hexdigest(), len(data) if size is None else size)


def test_artifacts_ok() -> None:
    assert verify_artifacts([_row("a", b"x")], FakeReader({"a": b"x"})) == []


def test_artifact_missing_hash_and_size_mismatch() -> None:
    reader = FakeReader({"b": b"tampered", "c": b"abc"})
    rows = [_row("a", b"x"), _row("b", b"orig"), _row("c", b"abc", size=99)]
    messages = {(f.subject, f.message.split(":")[0]) for f in verify_artifacts(rows, reader)}
    assert ("a", "object missing from store") in messages
    assert ("b", "sha256 mismatch") in messages
    assert ("c", "size mismatch") in messages


def test_artifact_unrecorded_size_is_not_checked() -> None:
    row = ArtifactRow("a", hashlib.sha256(b"x").hexdigest(), None)
    assert verify_artifacts([row], FakeReader({"a": b"x"})) == []


def test_local_reader_rejects_traversal_and_foreign_scheme(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_bytes(b"{}")
    reader = LocalArtifactReader(tmp_path)
    assert reader.read_bytes("local://ok.json") == b"{}"
    assert reader.read_bytes("local://nope.json") is None
    findings = verify_artifacts(
        [
            ArtifactRow("local://../etc/passwd", "0" * 64, None),
            ArtifactRow("s3://b/k", "0" * 64, None),
        ],
        reader,
    )
    assert len(findings) == 2 and all("unreadable" in f.message for f in findings)


def test_migrations() -> None:
    avail = {"001": "a", "002": "b", "003": "c"}
    assert verify_migrations(avail, avail) == []
    findings = verify_migrations({"001": "a", "002": "X", "999": "z"}, avail)
    assert {(f.subject, f.message.split(" ")[0]) for f in findings} == {
        ("002", "checksum"),
        ("003", "pending"),
        ("999", "applied"),
    }
    assert verify_migrations({"001": "a"}, avail, allow_pending=True) == []


def test_audit_log_state() -> None:
    ok = {
        "force_rls": True,
        "is_superuser": False,
        "can_update": False,
        "can_delete": False,
        "can_truncate": False,
    }
    assert verify_audit_log(**ok) == []
    assert len(verify_audit_log(**{**ok, "force_rls": False})) == 1
    assert len(verify_audit_log(**{**ok, "can_update": True, "can_delete": True})) == 2
    # a superuser (e.g. the migrator used for the drill) inherently holds the privileges
    assert verify_audit_log(**{**ok, "is_superuser": True, "can_update": True}) == []
