from uuid import UUID

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from apps.common import create_service, require
from archontos.authz import Permission
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.graph.hyperedges import (
    HyperedgeMember,
    MemoryHyperedgeStore,
    PostgresHyperedgeStore,
)
from archontos.projection.embedder import Embedder, build_embedder
from archontos.projection.search import MAX_LIMIT, similar
from archontos.projection.worker import ProjectionWorker

app = create_service("projection")
_hyperedges = MemoryHyperedgeStore()

POSTGRES_REQUIRED = "{what} requires ARCHONTOS_ACTION_BACKEND=postgres"


class HyperedgeMemberRequest(BaseModel):
    role: str = Field(min_length=1)
    ref_type: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ordinal: int = Field(default=0, ge=0)


class SimilarityRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)
    source_type: str | None = Field(default=None, min_length=1)
    min_score: float | None = Field(default=None, ge=-1, le=1)


class HyperedgeRequest(BaseModel):
    hyperedge_type: str = Field(min_length=1)
    members: list[HyperedgeMemberRequest] = Field(min_length=1)
    properties: dict = Field(default_factory=dict)


_embedder_cache: dict[tuple[str, str | None, str | None], Embedder | None] = {}


def _embedder() -> Embedder | None:
    settings = get_settings()
    key = (settings.embedder, settings.embedding_model, settings.embedding_cache_dir)
    if key not in _embedder_cache:
        # Model load is expensive; build once per configuration.
        _embedder_cache[key] = build_embedder(*key)
    return _embedder_cache[key]


def _worker() -> ProjectionWorker:
    return ProjectionWorker(get_session_factory(), embedder=_embedder())


def _require_postgres(what: str) -> None:
    if get_settings().action_backend != "postgres":
        raise HTTPException(status_code=409, detail=POSTGRES_REQUIRED.format(what=what))


def _hyperedge_store():
    if get_settings().action_backend == "postgres":
        return PostgresHyperedgeStore(get_session_factory())
    return _hyperedges


@app.get("/v1/status", dependencies=[require(Permission.QUERY_READ)])
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
        return await _worker().status()
    except (SQLAlchemyError, OSError):
        # Status is informational; an unreachable database degrades it, not the service.
        return {**body, "checkpoint_error": "database unavailable"}


@app.post("/v1/projections/drain", dependencies=[require(Permission.PROJECTION_ADMIN)])
async def drain_projection(limit: int = Query(default=100, ge=1, le=1000)):
    _require_postgres("projection drain")
    result = await _worker().process_batch(limit=limit)
    return {
        "projection": result.projection,
        "applied": result.applied,
        "last_sequence": result.last_sequence,
    }


@app.post("/v1/projections/rebuild", dependencies=[require(Permission.PROJECTION_ADMIN)])
async def rebuild_projection(limit: int = Query(default=500, ge=1, le=5000)):
    _require_postgres("projection rebuild")
    result = await _worker().rebuild(limit=limit)
    return {
        "projection": result.projection,
        "applied": result.applied,
        "last_sequence": result.last_sequence,
        "reset": result.reset,
    }


@app.post("/v1/hyperedges", dependencies=[require(Permission.HYPEREDGE_WRITE)])
async def create_hyperedge(payload: HyperedgeRequest):
    members = [
        HyperedgeMember(
            role=item.role, ref_type=item.ref_type, ref_id=item.ref_id, ordinal=item.ordinal
        )
        for item in payload.members
    ]
    created = await _hyperedge_store().create(payload.hyperedge_type, members, payload.properties)
    return created.as_api()


@app.get("/v1/hyperedges/{hyperedge_id}", dependencies=[require(Permission.ACTION_READ)])
async def get_hyperedge(hyperedge_id: str):
    store = _hyperedge_store()
    if isinstance(store, PostgresHyperedgeStore) and not _is_uuid(hyperedge_id):
        raise HTTPException(status_code=404, detail="hyperedge not found")
    found = await store.get(hyperedge_id)
    if found is None:
        raise HTTPException(status_code=404, detail="hyperedge not found")
    return found.as_api()


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


@app.post("/v1/search/similar", dependencies=[require(Permission.SEARCH_QUERY)])
async def similar_search(payload: SimilarityRequest):
    _require_postgres("similarity search")
    embedder = _embedder()
    if embedder is None:
        raise HTTPException(
            status_code=409,
            detail="similarity search requires ARCHONTOS_EMBEDDER (hashing|fastembed)",
        )
    if not payload.query.strip():
        raise HTTPException(status_code=422, detail="query must not be blank")
    hits = await similar(
        get_session_factory(),
        embedder,
        payload.query,
        limit=payload.limit,
        source_type=payload.source_type,
        min_score=payload.min_score,
    )
    return {"embedding_model": embedder.model_id, "hits": [hit.as_api() for hit in hits]}
