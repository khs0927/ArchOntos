from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.projection.base import ProjectionEvent
from archontos.projection.embedding import EmbeddingTextProjection


@dataclass(slots=True)
class ProjectionBatchResult:
    projection: str
    applied: int
    last_sequence: int
    reset: bool = False


class ProjectionWorker:
    """Drain domain_event into embedding_projection and advance the checkpoint.

    Normalization keeps its own outbox consumer. This worker is the projection
    drainer: it reads the immutable event log, not a second bus.
    """

    def __init__(
        self, session_factory: async_sessionmaker, projection: EmbeddingTextProjection | None = None
    ):
        self.session_factory = session_factory
        self.projection = projection or EmbeddingTextProjection()

    async def process_batch(self, limit: int = 100) -> ProjectionBatchResult:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        async with self.session_factory() as session:
            async with session.begin():
                checkpoint = await session.execute(
                    text(
                        """
                        INSERT INTO projection_checkpoint(projection_name, last_sequence)
                        VALUES (:name, 0)
                        ON CONFLICT (projection_name) DO UPDATE
                          SET projection_name = EXCLUDED.projection_name
                        RETURNING last_sequence
                        """
                    ),
                    {"name": self.projection.name},
                )
                last_sequence = int(checkpoint.scalar_one())
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT sequence, event_type, aggregate_type, aggregate_id, payload_json
                            FROM domain_event
                            WHERE sequence > :last_sequence
                            ORDER BY sequence
                            LIMIT :limit
                            """
                        ),
                        {"last_sequence": last_sequence, "limit": limit},
                    )
                ).all()
                applied = 0
                for row in rows:
                    event = ProjectionEvent(
                        sequence=int(row.sequence),
                        event_type=row.event_type,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        payload=dict(row.payload_json),
                    )
                    await self.projection.apply(event)
                    # Pop, so a long-lived worker does not accumulate every event in memory.
                    projected = self.projection.rows.pop(
                        f"{event.aggregate_type}:{event.aggregate_id}:{event.sequence}"
                    )
                    await session.execute(
                        text(
                            """
                            INSERT INTO embedding_projection(
                                projection_key, source_type, source_id, content,
                                canonical_updated_at
                            )
                            VALUES (
                                :key, :source_type, :source_id, :content, now()
                            )
                            ON CONFLICT (projection_key) DO UPDATE
                              SET content = EXCLUDED.content,
                                  projected_at = now()
                            """
                        ),
                        {
                            "key": projected.projection_key,
                            "source_type": projected.source_type,
                            "source_id": projected.source_id,
                            "content": projected.content,
                        },
                    )
                    last_sequence = event.sequence
                    applied += 1
                await session.execute(
                    text(
                        """
                        UPDATE projection_checkpoint
                        SET last_sequence = :last_sequence, updated_at = now()
                        WHERE projection_name = :name
                        """
                    ),
                    {"name": self.projection.name, "last_sequence": last_sequence},
                )
                if applied:
                    await session.execute(
                        text(
                            """
                            UPDATE outbox_message
                            SET status = 'published', published_at = now()
                            WHERE status = 'pending'
                              AND topic LIKE 'projection.%'
                              AND event_id IN (
                                SELECT event_id FROM domain_event
                                WHERE sequence <= :last_sequence
                              )
                            """
                        ),
                        # Only acknowledge messages whose events this batch actually covered.
                        {"last_sequence": last_sequence},
                    )
        return ProjectionBatchResult(
            projection=self.projection.name,
            applied=applied,
            last_sequence=last_sequence,
        )

    async def rebuild(self, limit: int = 500) -> ProjectionBatchResult:
        async with self.session_factory() as session:
            async with session.begin():
                await self.projection.reset()
                await session.execute(text("DELETE FROM embedding_projection"))
                await session.execute(
                    text(
                        """
                        INSERT INTO projection_checkpoint(projection_name, last_sequence)
                        VALUES (:name, 0)
                        ON CONFLICT (projection_name) DO UPDATE
                          SET last_sequence = 0, updated_at = now()
                        """
                    ),
                    {"name": self.projection.name},
                )
        # Replay the whole log in ``limit``-sized transactions so a rebuild is complete.
        applied = 0
        while True:
            batch = await self.process_batch(limit=limit)
            applied += batch.applied
            if batch.applied < limit:
                break
        return ProjectionBatchResult(
            projection=self.projection.name,
            applied=applied,
            last_sequence=batch.last_sequence,
            reset=True,
        )

    async def status(self) -> dict:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT last_sequence, updated_at
                        FROM projection_checkpoint
                        WHERE projection_name = :name
                        """
                    ),
                    {"name": self.projection.name},
                )
            ).first()
        return {
            "canonical_source": "postgresql",
            "projections": [self.projection.name, "pgvector"],
            "rebuildable": True,
            "event_transport": "transactional-outbox",
            "checkpoint": None
            if row is None
            else {
                "last_sequence": int(row.last_sequence),
                "updated_at": row.updated_at.isoformat(),
            },
        }


def event_content(event: ProjectionEvent) -> str:
    return json.dumps(
        {
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "payload": event.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
