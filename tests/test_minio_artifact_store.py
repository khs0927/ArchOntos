"""MinioArtifactStore against an in-memory client double (no server, no network)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from typing import Any

import pytest

from archontos.storage.artifacts import ArtifactReadError, MinioArtifactStore


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeStat:
    def __init__(self, size: int) -> None:
        self.size = size


class FakeMinio:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.responses: list[FakeResponse] = []

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def stat_object(self, bucket: str, key: str) -> FakeStat:
        if (bucket, key) not in self.objects:
            raise FakeS3Error("NoSuchKey")
        return FakeStat(len(self.objects[(bucket, key)]))

    def put_object(
        self, bucket: str, key: str, data: io.BytesIO, *, length: int, content_type: str
    ) -> None:
        body = data.read()
        assert len(body) == length
        self.objects[(bucket, key)] = body

    def get_object(self, bucket: str, key: str) -> FakeResponse:
        if (bucket, key) not in self.objects:
            raise FakeS3Error("NoSuchKey")
        response = FakeResponse(self.objects[(bucket, key)])
        self.responses.append(response)
        return response


def _store() -> tuple[MinioArtifactStore, FakeMinio]:
    store = object.__new__(MinioArtifactStore)  # skip the real minio client
    fake = FakeMinio()
    store.client = fake  # type: ignore[assignment]
    store.bucket = "archontos"
    return store, fake


def test_put_bytes_then_get_bytes_roundtrip() -> None:
    store, fake = _store()
    payload = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    ref = asyncio.run(store.put_bytes("cad", payload, mime="application/dxf", suffix=".dxf"))
    digest = hashlib.sha256(payload).hexdigest()
    assert ref.storage_uri == f"s3://archontos/raw/cad/{digest[:2]}/{digest}.dxf"
    assert ref.content_hash == digest and ref.byte_size == len(payload)
    assert asyncio.run(store.get_bytes(ref.storage_uri)) == payload
    assert all(r.closed and r.released for r in fake.responses)
    # idempotent: the same content is not written twice and keeps the same URI
    again = asyncio.run(store.put_bytes("cad", payload, mime="application/dxf", suffix=".dxf"))
    assert again == ref and len(fake.objects) == 1


def test_get_bytes_rejects_foreign_bucket_scheme_and_empty_key() -> None:
    store, _ = _store()
    for uri in (
        "s3://other-bucket/raw/x.dxf",
        "local://raw/x.dxf",
        "https://archontos/raw/x.dxf",
        "s3://archontos/",
    ):
        with pytest.raises(ArtifactReadError):
            asyncio.run(store.get_bytes(uri))


def test_get_bytes_missing_object_is_artifact_read_error() -> None:
    store, _ = _store()
    with pytest.raises(ArtifactReadError):
        asyncio.run(store.get_bytes("s3://archontos/raw/cad/aa/missing.dxf"))


def test_get_bytes_read_failure_releases_connection() -> None:
    store, fake = _store()
    fake.objects[("archontos", "raw/a.bin")] = b"x"

    class Broken(FakeResponse):
        def read(self) -> bytes:
            raise OSError("connection reset")

    broken = Broken(b"x")
    fake.get_object = lambda bucket, key: broken  # type: ignore[method-assign]
    with pytest.raises(ArtifactReadError):
        asyncio.run(store.get_bytes("s3://archontos/raw/a.bin"))
    assert broken.closed and broken.released


def test_get_json_roundtrip_and_errors() -> None:
    store, fake = _store()
    fake.objects[("archontos", "raw/j.json")] = json.dumps({"a": 1}).encode()
    fake.objects[("archontos", "raw/list.json")] = b"[1]"
    fake.objects[("archontos", "raw/bad.json")] = b"{not json"
    fake.objects[("archontos", "raw/bin.json")] = b"\xff\xfe"
    assert asyncio.run(store.get_json("s3://archontos/raw/j.json")) == {"a": 1}
    for uri in (
        "s3://archontos/raw/list.json",
        "s3://archontos/raw/bad.json",
        "s3://archontos/raw/bin.json",
        "s3://archontos/raw/missing.json",
        "s3://other/raw/j.json",
    ):
        with pytest.raises(ArtifactReadError):
            asyncio.run(store.get_json(uri))


def test_collision_when_same_key_has_other_size() -> None:
    store, fake = _store()
    payload = b"abc"
    digest = hashlib.sha256(payload).hexdigest()
    fake.buckets.add("archontos")
    fake.objects[("archontos", f"raw/cad/{digest[:2]}/{digest}.dxf")] = b"abcd"
    kwargs: dict[str, Any] = {"mime": "application/dxf", "suffix": ".dxf"}
    with pytest.raises(RuntimeError, match="collision"):
        asyncio.run(store.put_bytes("cad", payload, **kwargs))
