from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from archontos.ingestion.persistence import CanonicalizationError
from archontos.rules.engine import RuleEvaluationError


async def _unprocessable(_request: Request, exc: Exception) -> JSONResponse:
    # Invalid rule documents and non-canonicalizable source records are client/data errors that
    # must fail closed with a reason, not surface as an opaque HTTP 500.
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def create_service(name: str) -> FastAPI:
    app = FastAPI(title=f"ArchOntos {name}", version="0.1.0")
    app.add_exception_handler(RuleEvaluationError, _unprocessable)
    app.add_exception_handler(CanonicalizationError, _unprocessable)

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
