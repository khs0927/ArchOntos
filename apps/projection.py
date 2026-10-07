from fastapi import HTTPException
from pydantic import BaseModel, Field

from apps.common import create_service
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.graph.hyperedges import HyperedgeMember, MemoryHyperedgeStore, PostgresHyperedgeStore
from archontos.projection.worker import ProjectionWorker

app = create_service("projection")
_hyperedges = MemoryHyperedgeStore()


class HyperedgeRequest(BaseModel):
    hyperedge_type: str = Field(min_length=1)
    members: list[dict] = Field(min_length=1)
    properties: dict = Field(default_factory=dict)


def _worker() -> ProjectionWorker:
    return ProjectionWorker(get_session_factory())


def _hyperedge_store():
    if get_settings().action_backend == "postgres":
        return PostgresHyperedgeStore(get_session_factory())
    return _hyperedges


@app.get("/v1/status")
async def projection_status():
    body = {
        "canonical_source": "postgresql",
        "projections": ["embedding_text", "pgvector"],
        "rebuildable": True,
        "event_transport": "transactional-outbox",
        "checkpoint": None,
    }
    if get_settings().action_backend != "postgres":
        return body
    try:
        live = await _worker().status()
    except Exception:
        return body
    return live


@app.post("/v1/projections/drain")
async def drain_projection(limit: int = 100):
    if get_settings().action_backend != "postgres":
        raise HTTPException(status_code=409, detail="projection drain requires ARCHONTOS_ACTION_BACKEND=postgres")
    result = await _worker().process_batch(limit=limit)
    return {
        "projection": result.projection,
        "applied": result.applied,
        "last_sequence": result.last_sequence,
    }


@app.post("/v1/projections/rebuild")
async def rebuild_projection(limit: int = 500):
    if get_settings().action_backend != "postgres":
        raise HTTPException(status_code=409, detail="projection rebuild requires ARCHONTOS_ACTION_BACKEND=postgres")
    result = await _worker().rebuild(limit=limit)
    return {
        "projection": result.projection,
        "applied": result.applied,
        "last_sequence": result.last_sequence,
        "reset": result.reset,
    }


@app.post("/v1/hyperedges")
async def create_hyperedge(payload: HyperedgeRequest):
    members = [
        HyperedgeMember(
            role=str(item["role"]),
            ref_type=str(item["ref_type"]),
            ref_id=str(item["ref_id"]),
            ordinal=int(item.get("ordinal", 0)),
        )
        for item in payload.members
    ]
    created = await _hyperedge_store().create(payload.hyperedge_type, members, payload.properties)
    return created.as_api()
