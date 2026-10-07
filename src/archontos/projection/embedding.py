"""Rebuildable text projection over canonical domain events.

PostgreSQL remains canonical. ``embedding_projection`` is a deletable view:
reset clears it and the checkpoint, then events are replayed from sequence 0.
Embeddings stay null until an embedder is configured; content is the rebuild key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from archontos.projection.base import ProjectionEvent, RebuildableProjection


@dataclass(slots=True)
class ProjectedRow:
    projection_key: str
    source_type: str
    source_id: str
    content: str


class EmbeddingTextProjection(RebuildableProjection):
    name = "embedding_text"

    def __init__(self) -> None:
        self.rows: dict[str, ProjectedRow] = {}

    async def apply(self, event: ProjectionEvent) -> None:
        content = json.dumps(
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
        key = f"{event.aggregate_type}:{event.aggregate_id}:{event.sequence}"
        self.rows[key] = ProjectedRow(
            projection_key=key,
            source_type=event.aggregate_type,
            source_id=event.aggregate_id,
            content=content,
        )

    async def reset(self) -> None:
        self.rows.clear()
