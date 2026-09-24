from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response


def create_service(name: str) -> FastAPI:
    app = FastAPI(title=f"ArchOntos {name}", version="0.1.0")

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
