from apps.common import create_service

app = create_service("projection")


@app.get("/v1/status")
async def projection_status():
    return {
        "canonical_source": "postgresql",
        "projections": ["pgvector"],
        "rebuildable": True,
        "event_transport": "transactional-outbox",
    }
