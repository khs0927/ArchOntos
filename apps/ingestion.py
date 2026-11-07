import asyncio
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError

from apps.common import create_service, require
from archontos.authz import Permission
from archontos.cad.dxf import CadDependencyError, CadParseError, parse_dxf
from archontos.cad.persistence import CadConflict, CadRepository, InvalidCadCursor, facts_export
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.identity import current_jurisdictions
from archontos.ingestion.adapters import LawGoKrAdapter, SourceAdapterError
from archontos.ingestion.contracts import NormalizedLegalVersion
from archontos.ingestion.persistence import CanonicalizationError, SourceVersionConflict
from archontos.ingestion.service import LawIngestionService, LawNotFoundError
from archontos.storage.artifacts import LocalArtifactStore, MinioArtifactStore

app = create_service("ingestion")
settings = get_settings()


def _lawgo() -> LawGoKrAdapter:
    return LawGoKrAdapter(base_url=settings.lawgo_base_url, oc=settings.lawgo_oc)


def _artifact_store():
    if settings.artifact_backend == "minio":
        return MinioArtifactStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    if settings.artifact_backend == "local":
        return LocalArtifactStore(settings.artifact_local_path)
    raise RuntimeError(f"unsupported artifact backend: {settings.artifact_backend}")


@app.post("/v1/contracts/legal-version/validate", dependencies=[require(Permission.QUERY_READ)])
async def validate_legal_version(payload: NormalizedLegalVersion):
    return {
        "valid": True,
        "source_key": payload.source_key,
        "evidence_count": len(payload.evidence),
    }


@app.get("/v1/sources/lawgo/search", dependencies=[require(Permission.QUERY_READ)])
async def search_lawgo(
    query: Annotated[str, Query(min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    display: Annotated[int, Query(ge=1, le=100)] = 20,
):
    try:
        result = await _lawgo().search_laws(query, page=page, display=display)
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "target": result.target,
        "query": result.query,
        "page": result.page,
        "total_count": result.total_count,
        "artifact_sha256": result.envelope.sha256,
        "items": [asdict(item) for item in result.items],
    }


@app.get("/v1/sources/lawgo/effective-versions", dependencies=[require(Permission.QUERY_READ)])
async def search_lawgo_effective_versions(
    query: Annotated[str, Query(min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    display: Annotated[int, Query(ge=1, le=100)] = 100,
):
    try:
        result = await _lawgo().search_effective_versions(
            query,
            page=page,
            display=display,
        )
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "target": result.target,
        "query": result.query,
        "page": result.page,
        "total_count": result.total_count,
        "artifact_sha256": result.envelope.sha256,
        "items": [asdict(item) for item in result.items],
    }


@app.post("/v1/sources/lawgo/ingest-current", dependencies=[require(Permission.SOURCE_INGEST)])
async def ingest_lawgo_current(query: Annotated[str, Query(min_length=1)]):
    service = LawIngestionService(
        adapter=_lawgo(),
        artifact_store=_artifact_store(),
        session_factory=get_session_factory(),
    )
    try:
        result = await service.ingest_current(query)
    except LawNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SourceVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CanonicalizationError as exc:
        # A malformed or unverifiable official document is a bad request, not a
        # server fault. It must not be retried either: the input is not
        # transient, and retrying re-fetches the same wrong body.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "law_id": result.item.law_id,
        "mst": result.item.mst,
        "source_version_id": str(result.persisted.source_version_id),
        "created": result.persisted.created,
        "body_sha256": result.body_sha256,
        "discovery_sha256": result.discovery_sha256,
    }


# --- MVP-1: CAD (DXF) drawings -> raw entities -> architectural objects -------------------

CAD_MAX_BYTES = 50 * 1024 * 1024
_SOURCE_TEXT = r"^[^\x00-\x1f]{1,300}$"


@app.post("/v1/cad/artifacts", status_code=201, dependencies=[require(Permission.CAD_INGEST)])
async def upload_cad_artifact(
    request: Request,
    response: Response,
    project_id: Annotated[str, Query(min_length=1, max_length=200, pattern=_SOURCE_TEXT)],
    source_key: Annotated[str, Query(min_length=1, max_length=300, pattern=_SOURCE_TEXT)],
    jurisdiction_code: Annotated[str, Query(min_length=2, max_length=20)] = "KR",
    revision_label: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    file_name: Annotated[str | None, Query(max_length=255)] = None,
    assume_units: Annotated[str, Query(pattern=r"^(mm|cm|m|in|ft)$")] = "mm",
):
    """Upload DXF bytes (request body); returns the revision. Same bytes -> same ids (200)."""
    if jurisdiction_code not in current_jurisdictions():
        raise HTTPException(status_code=403, detail="jurisdiction not allowed for this caller")
    data = await request.body()
    if len(data) > CAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="DXF upload exceeds 50 MiB")
    try:
        drawing = await asyncio.to_thread(parse_dxf, data, assume_units=assume_units)
    except CadParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CadDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    artifact = await _artifact_store().put_bytes("cad", data, mime="image/vnd.dxf", suffix=".dxf")
    async with get_session_factory()() as session:
        try:
            async with session.begin():
                result = await CadRepository(session).persist(
                    drawing=drawing,
                    artifact=artifact,
                    project_id=project_id,
                    source_key=source_key,
                    jurisdiction_code=jurisdiction_code,
                    revision_label=revision_label,
                    file_name=file_name,
                )
        except CadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="CAD revision conflict") from exc
    if not result.created:
        response.status_code = 200
    return {"created": result.created, "revision": result.revision}


def _cad_cursor_error(exc: InvalidCadCursor) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/cad/revisions", dependencies=[require(Permission.CAD_READ)])
async def list_cad_revisions(
    project_id: str | None = None,
    source_key: str | None = None,
    sha256: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        try:
            items, nxt = await CadRepository(session).list_revisions(
                project_id=project_id,
                source_key=source_key,
                sha256=sha256,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


async def _revision_or_404(repo: CadRepository, revision_id: UUID) -> dict:
    revision = await repo.get_revision(str(revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="CAD revision not found")
    return revision


@app.get("/v1/cad/revisions/{revision_id}", dependencies=[require(Permission.CAD_READ)])
async def get_cad_revision(revision_id: UUID):
    async with get_session_factory()() as session, session.begin():
        return await _revision_or_404(CadRepository(session), revision_id)


@app.get("/v1/cad/revisions/{revision_id}/objects", dependencies=[require(Permission.CAD_READ)])
async def list_cad_objects(
    revision_id: UUID,
    kind: Annotated[
        str | None, Query(pattern=r"^(wall|door|window|space|stair|column|opening)$")
    ] = None,
    layer: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        await _revision_or_404(repo, revision_id)
        try:
            items, nxt = await repo.list_objects(
                str(revision_id), kind=kind, layer=layer, limit=limit, cursor=cursor
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


@app.get("/v1/cad/revisions/{revision_id}/entities", dependencies=[require(Permission.CAD_READ)])
async def list_cad_entities(
    revision_id: UUID,
    layer: str | None = None,
    entity_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        await _revision_or_404(repo, revision_id)
        try:
            items, nxt = await repo.list_entities(
                str(revision_id),
                layer=layer,
                entity_type=entity_type,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


@app.get("/v1/cad/revisions/{revision_id}/facts", dependencies=[require(Permission.CAD_READ)])
async def cad_revision_facts(revision_id: UUID):
    """The revision as ``aec-facts-export/1`` (input of archontos.integration.ontology)."""
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        revision = await _revision_or_404(repo, revision_id)
        objects = await repo.all_objects(str(revision_id))
    return facts_export(revision, objects)
