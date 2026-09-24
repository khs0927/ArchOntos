from dataclasses import asdict

from fastapi import HTTPException, Query

from apps.common import create_service
from archontos.config import get_settings
from archontos.ingestion.adapters import LawGoKrAdapter, SourceAdapterError
from archontos.ingestion.contracts import NormalizedLegalVersion

app = create_service("ingestion")
settings = get_settings()


def _lawgo() -> LawGoKrAdapter:
    return LawGoKrAdapter(base_url=settings.lawgo_base_url, oc=settings.lawgo_oc)


@app.post("/v1/contracts/legal-version/validate")
async def validate_legal_version(payload: NormalizedLegalVersion):
    return {
        "valid": True,
        "source_key": payload.source_key,
        "evidence_count": len(payload.evidence),
    }


@app.get("/v1/sources/lawgo/search")
async def search_lawgo(
    query: str = Query(min_length=1),
    page: int = Query(default=1, ge=1),
    display: int = Query(default=20, ge=1, le=100),
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


@app.get("/v1/sources/lawgo/effective-versions")
async def search_lawgo_effective_versions(
    query: str = Query(min_length=1),
    page: int = Query(default=1, ge=1),
    display: int = Query(default=100, ge=1, le=100),
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
