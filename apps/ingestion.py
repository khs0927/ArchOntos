from dataclasses import asdict
from typing import Annotated

from fastapi import HTTPException, Query

from apps.common import create_service, require
from archontos.authz import Permission
from archontos.config import get_settings
from archontos.db.session import get_session_factory
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
