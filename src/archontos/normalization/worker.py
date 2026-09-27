from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.ingestion.adapters import LawGoKrAdapter, RawSourceEnvelope
from archontos.normalization.legal import LawEvidenceNormalizer
from archontos.normalization.persistence import CanonicalEvidenceRepository
from archontos.storage.artifacts import ArtifactStore


@dataclass(frozen=True, slots=True)
class NormalizationWorkResult:
    outbox_id: int
    source_version_id: UUID | None
    status: str
    attempts: int
    evidence_count: int = 0
    error: str | None = None


class NormalizationOutboxWorker:
    """Consume canonical source-version outbox messages one at a time."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        artifact_store: ArtifactStore,
        max_attempts: int = 5,
    ):
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.max_attempts = max_attempts
        self.normalizer = LawEvidenceNormalizer()

    async def process_one(self) -> NormalizationWorkResult | None:
        async with self.session_factory() as session:
            async with session.begin():
                claimed = await session.execute(
                    text(
                        """
                        SELECT id, payload_json, attempts
                        FROM outbox_message
                        WHERE topic = 'normalization.source-version'
                          AND status = 'pending'
                        ORDER BY id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                )
                row = claimed.first()
                if row is None:
                    return None

                source_version_id: UUID | None = None
                attempts = int(row.attempts) + 1
                try:
                    source_version_id = UUID(str(row.payload_json["source_version_id"]))
                    async with session.begin_nested():
                        evidence_count = await self._normalize_source_version(
                            session,
                            source_version_id,
                        )
                # BLE001: deliberate. This is the worker's transaction boundary.
                # The savepoint rolled back, the error is written to
                # outbox_message.last_error, the row flips to pending or failed
                # at max_attempts, and a high-severity quality_flag is recorded.
                # A narrower tuple would let an unexpected exception escape with
                # the outbox row left claimed, which is the worse outcome.
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)[:1000]
                    status = "failed" if attempts >= self.max_attempts else "pending"
                    await session.execute(
                        text(
                            """
                            UPDATE outbox_message
                            SET status = :status,
                                attempts = :attempts,
                                last_error = :last_error,
                                last_attempt_at = now()
                            WHERE id = :outbox_id
                            """
                        ),
                        {
                            "outbox_id": row.id,
                            "status": status,
                            "attempts": attempts,
                            "last_error": error,
                        },
                    )
                    if status == "failed":
                        await session.execute(
                            text(
                                """
                                INSERT INTO quality_flag(
                                    target_table, target_id, flag_type, severity, message
                                )
                                VALUES (
                                    :target_table, :target_id,
                                    'contract_violation', 'high', :message
                                )
                                """
                            ),
                            {
                                "target_table": (
                                    "source_version"
                                    if source_version_id is not None
                                    else "outbox_message"
                                ),
                                "target_id": source_version_id,
                                "message": (
                                    f"normalization outbox {row.id} failed after retries: {error}"
                                ),
                            },
                        )
                    return NormalizationWorkResult(
                        outbox_id=row.id,
                        source_version_id=source_version_id,
                        status=status,
                        attempts=attempts,
                        error=error,
                    )

                await session.execute(
                    text(
                        """
                        UPDATE outbox_message
                        SET status = 'published',
                            attempts = :attempts,
                            last_error = NULL,
                            last_attempt_at = now(),
                            published_at = now()
                        WHERE id = :outbox_id
                        """
                    ),
                    {"outbox_id": row.id, "attempts": attempts},
                )
                await self._record_normalized_event(
                    session,
                    source_version_id=source_version_id,
                    evidence_count=evidence_count,
                )
                return NormalizationWorkResult(
                    outbox_id=row.id,
                    source_version_id=source_version_id,
                    status="published",
                    attempts=attempts,
                    evidence_count=evidence_count,
                )

    async def _normalize_source_version(self, session, source_version_id: UUID) -> int:
        source_result = await session.execute(
            text(
                """
                SELECT
                    sv.raw_manifest_json,
                    a.storage_uri,
                    a.content_hash
                FROM source_version sv
                JOIN artifact a ON a.id = sv.artifact_id
                WHERE sv.id = :source_version_id
                """
            ),
            {"source_version_id": source_version_id},
        )
        source_row = source_result.first()
        if source_row is None:
            raise RuntimeError(f"unknown source_version_id: {source_version_id}")

        manifest = dict(source_row.raw_manifest_json)
        source_name = str(manifest.get("source") or "")
        if source_name != "law.go.kr":
            raise RuntimeError(f"unsupported normalization source: {source_name!r}")

        payload = await self.artifact_store.get_json(source_row.storage_uri)
        canonical_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        if digest != source_row.content_hash:
            raise RuntimeError(
                "artifact integrity mismatch for "
                f"source_version {source_version_id}: "
                f"expected {source_row.content_hash}, got {digest}"
            )

        params = {
            "ID": manifest.get("law_id"),
            "MST": manifest.get("mst"),
        }
        envelope = RawSourceEnvelope(
            source_name=source_name,
            endpoint=source_row.storage_uri,
            params={key: value for key, value in params.items() if value},
            payload=payload,
            fetched_at=datetime.now(UTC),
        )
        body = LawGoKrAdapter.parse_body(envelope)
        units = self.normalizer.normalize(body)
        persisted = await CanonicalEvidenceRepository(session).persist(
            source_version_id=source_version_id,
            units=units,
        )
        return persisted.count

    @staticmethod
    async def _record_normalized_event(
        session,
        *,
        source_version_id: UUID,
        evidence_count: int,
    ) -> None:
        event_payload = {
            "source_version_id": str(source_version_id),
            "evidence_count": evidence_count,
        }
        await session.execute(
            text(
                """
                INSERT INTO domain_event(
                    aggregate_type, aggregate_id, event_type, payload_json
                )
                VALUES (
                    'source_version', :aggregate_id, 'SourceVersionNormalized',
                    CAST(:payload_json AS jsonb)
                )
                """
            ),
            {
                "aggregate_id": str(source_version_id),
                "payload_json": json.dumps(event_payload, sort_keys=True),
            },
        )
