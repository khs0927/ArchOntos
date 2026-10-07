import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from archontos.config import get_settings
from archontos.ingestion.persistence import CanonicalizationError
from archontos.observability import REQUEST_COUNT
from archontos.rules.engine import RuleEvaluationError

OPEN_PATHS = {"/health", "/metrics"}


def api_keys() -> list[str]:
    return [item.strip() for item in get_settings().api_keys.split(",") if item.strip()]


def key_matches(presented: str, keys: list[str]) -> bool:
    """Compare in constant time against every configured key (no early exit)."""
    encoded = presented.encode()
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(encoded, key.encode())
    return matched


def route_label(request: Request) -> str:
    """Use the route template (``/v1/actions/{action_id}/approve``), never the raw path.

    Raw paths embed ids, which would give the Prometheus counter unbounded cardinality.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


async def _unprocessable(_request: Request, exc: Exception) -> JSONResponse:
    # Invalid rule documents and non-canonicalizable source records are client/data errors that
    # must fail closed with a reason, not surface as an opaque HTTP 500.
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def create_service(name: str) -> FastAPI:
    app = FastAPI(title=f"ArchOntos {name}", version="0.1.0")
    app.add_exception_handler(RuleEvaluationError, _unprocessable)
    app.add_exception_handler(CanonicalizationError, _unprocessable)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        keys = api_keys()
        if keys and request.url.path not in OPEN_PATHS:
            presented = request.headers.get("x-api-key", "")
            if not key_matches(presented, keys):
                REQUEST_COUNT.labels(service=name, route="unauthorized").inc()
                return JSONResponse(
                    status_code=401, content={"detail": "missing or invalid api key"}
                )
        response = await call_next(request)
        REQUEST_COUNT.labels(service=name, route=route_label(request)).inc()
        return response

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
