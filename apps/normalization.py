from uuid import UUID

from fastapi import HTTPException

from apps.common import create_service, require
from archontos.assertions.contracts import (
    AssertionCandidateCreate,
    AssertionCandidateView,
    AssertionReviewRequest,
    AssertionReviewView,
)
from archontos.assertions.persistence import (
    AssertionNotFoundError,
    EvidenceNotFoundError,
)
from archontos.assertions.review import AssertionReviewError, promotable_to_rule
from archontos.assertions.service import AssertionWorkflowService
from archontos.authz import Permission
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.domain.contracts import AssertionContract, EvidenceSpanContract
from archontos.domain.enums import ReviewStatus
from archontos.normalization.worker import NormalizationOutboxWorker
from archontos.storage.artifacts import LocalArtifactStore, MinioArtifactStore

app = create_service("normalization")
settings = get_settings()


def _workflow() -> AssertionWorkflowService:
    return AssertionWorkflowService(session_factory=get_session_factory())


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


@app.post("/v1/contracts/assertion/validate", dependencies=[require(Permission.QUERY_READ)])
async def validate_assertion(evidence: EvidenceSpanContract, assertion: AssertionContract):
    return {
        "valid": True,
        "extractor_method": evidence.extractor_method,
        "interpreter_method": assertion.interpreter_method,
        "review_status": assertion.review_status,
    }


@app.post("/v1/normalization/process-one", dependencies=[require(Permission.SOURCE_INGEST)])
async def process_normalization_outbox():
    result = await NormalizationOutboxWorker(
        session_factory=get_session_factory(),
        artifact_store=_artifact_store(),
    ).process_one()
    if result is None:
        return {"processed": False}
    return {
        "processed": True,
        "outbox_id": result.outbox_id,
        "source_version_id": (
            str(result.source_version_id) if result.source_version_id is not None else None
        ),
        "status": result.status,
        "attempts": result.attempts,
        "evidence_count": result.evidence_count,
        "error": result.error,
    }


@app.post(
    "/v1/assertions/candidates",
    dependencies=[require(Permission.ASSERTION_PROPOSE)],
    response_model=AssertionCandidateView,
)
async def create_assertion_candidate(payload: AssertionCandidateCreate):
    try:
        result = await _workflow().create_candidate(payload)
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AssertionCandidateView(
        assertion_id=result.assertion_id,
        source_version_id=result.source_version_id,
        evidence_span_id=result.evidence_span_id,
        assertion_key=result.assertion_key,
        review_status=result.review_status,
        created=result.created,
    )


@app.post(
    "/v1/assertions/{assertion_id}/review",
    dependencies=[require(Permission.ASSERTION_REVIEW)],
    response_model=AssertionReviewView,
)
async def review_assertion(assertion_id: UUID, payload: AssertionReviewRequest):
    decision = ReviewStatus(payload.decision)
    try:
        result = await _workflow().review(
            assertion_id=assertion_id,
            decision=decision,
            reviewer_id=payload.reviewer_id,
            note=payload.note,
        )
    except AssertionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssertionReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return AssertionReviewView(
        assertion_id=result.assertion_id,
        previous_status=result.previous_status,
        review_status=result.review_status,
        reviewer_id=result.reviewer_id,
        promotable_to_rule=promotable_to_rule(result.review_status),
    )
