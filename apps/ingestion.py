from apps.common import create_service
from archontos.ingestion.contracts import NormalizedLegalVersion

app = create_service("ingestion")


@app.post("/v1/contracts/legal-version/validate")
async def validate_legal_version(payload: NormalizedLegalVersion):
    return {
        "valid": True,
        "source_key": payload.source_key,
        "evidence_count": len(payload.evidence),
    }
