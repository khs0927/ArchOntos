from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from archontos.ingestion.adapters import RawSourceEnvelope


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_type: str
    mime: str
    storage_uri: str
    content_hash: str
    byte_size: int


class ArtifactStore(Protocol):
    async def put_envelope(self, envelope: RawSourceEnvelope) -> ArtifactRef: ...


class LocalArtifactStore:
    """Content-addressed artifact store for local development and tests."""

    def __init__(self, root: str | Path = "local-data/artifacts"):
        self.root = Path(root)

    async def put_envelope(self, envelope: RawSourceEnvelope) -> ArtifactRef:
        payload = envelope.canonical_bytes
        digest = envelope.sha256
        relative = Path("raw") / envelope.source_name / digest[:2] / f"{digest}.json"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != payload:
                raise RuntimeError(f"content-address collision at {path}")
        else:
            temp = path.with_suffix(".tmp")
            temp.write_bytes(payload)
            temp.replace(path)
        return ArtifactRef(
            artifact_type="json",
            mime="application/json",
            storage_uri=f"local://{relative.as_posix()}",
            content_hash=digest,
            byte_size=len(payload),
        )


class MinioArtifactStore:
    """S3-compatible immutable artifact store backed by MinIO."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = "archontos",
        secure: bool = False,
    ):
        from minio import Minio

        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket

    def _put_sync(self, envelope: RawSourceEnvelope) -> ArtifactRef:
        payload = envelope.canonical_bytes
        digest = envelope.sha256
        key = f"raw/{envelope.source_name}/{digest[:2]}/{digest}.json"
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        try:
            stat = self.client.stat_object(self.bucket, key)
            if stat.size != len(payload):
                raise RuntimeError(f"content-address collision at s3://{self.bucket}/{key}")
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise
            self.client.put_object(
                self.bucket,
                key,
                io.BytesIO(payload),
                length=len(payload),
                content_type="application/json",
            )
        return ArtifactRef(
            artifact_type="json",
            mime="application/json",
            storage_uri=f"s3://{self.bucket}/{key}",
            content_hash=digest,
            byte_size=len(payload),
        )

    async def put_envelope(self, envelope: RawSourceEnvelope) -> ArtifactRef:
        return await asyncio.to_thread(self._put_sync, envelope)
