from apps.common import create_service
from archontos.domain.contracts import AssertionContract, EvidenceSpanContract

app = create_service("normalization")


@app.post("/v1/contracts/assertion/validate")
async def validate_assertion(evidence: EvidenceSpanContract, assertion: AssertionContract):
    return {
        "valid": True,
        "extractor_method": evidence.extractor_method,
        "interpreter_method": assertion.interpreter_method,
        "review_status": assertion.review_status,
    }
