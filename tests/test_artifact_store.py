import asyncio
from datetime import datetime, timezone

from archontos.ingestion.adapters import RawSourceEnvelope
from archontos.storage.artifacts import LocalArtifactStore


def test_local_artifact_store_is_content_addressed_and_idempotent(tmp_path):
    envelope = RawSourceEnvelope(
        source_name="law.go.kr",
        endpoint="https://www.law.go.kr/DRF/lawService.do",
        params={"target": "law", "MST": "123"},
        payload={"법령": {"법령키": "123"}},
        fetched_at=datetime.now(timezone.utc),
    )
    store = LocalArtifactStore(tmp_path)
    first = asyncio.run(store.put_envelope(envelope))
    second = asyncio.run(store.put_envelope(envelope))
    assert first == second
    assert first.storage_uri.startswith("local://raw/law.go.kr/")
    assert len(first.content_hash) == 64
    stored = next(tmp_path.rglob("*.json"))
    assert stored.read_bytes() == envelope.canonical_bytes
