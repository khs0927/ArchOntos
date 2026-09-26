"""HTTP-level smoke tests for the MVP-0 service surface.

The canonical path was previously only exercised by calling repository classes
directly, so a route that raised on every request would still have passed CI.
These tests drive the real ASGI apps through ``TestClient`` and assert both the
happy path and the failure mapping, including one database-backed round trip.

The five apps each import ``get_session_factory`` into their own module
namespace and call it at request time, so pointing the module attribute at the
throwaway test schema is enough to route a request into the real database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import apps.action as action_app
import apps.ingestion as ingestion_app
import apps.normalization as normalization_app
import apps.projection as projection_app
import apps.rule_engine as rule_engine_app
from archontos.storage.artifacts import LocalArtifactStore

SERVICES = (
    (ingestion_app, "ingestion"),
    (normalization_app, "normalization"),
    (rule_engine_app, "rule-engine"),
    (action_app, "action"),
    (projection_app, "projection"),
)

LEGAL_VERSION_PAYLOAD = {
    "source_key": "law:001823",
    "title": "테스트 건축법",
    "issuer": "국토교통부",
    "jurisdiction_code": "KR",
    "document_type": "statute",
    "version_label": "999001",
    "effective_from": "2026-01-01",
    "artifact": {
        "source_name": "law.go.kr",
        "source_url": "https://www.law.go.kr/DRF/lawService.do?target=law&MST=999001",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "content_hash": "0123456789abcdef0123456789abcdef",
        "mime_type": "application/json",
        "storage_uri": "local-data/artifacts/0123456789abcdef0123456789abcdef.json",
    },
    "evidence": [
        {
            "evidence_key": "law:001823/10/1/1",
            "locator": {"kind": "subparagraph", "article": "10"},
            "text_snippet": "직통계단을 2개소 이상 설치한다.",
            "extractor_method": "structured-parser",
            "extraction_confidence": 1.0,
        }
    ],
}

ASSERTION_CONTRACT_PAYLOAD = {
    "evidence": {
        "evidence_key": "law:001823/10/1/1",
        "locator": {"kind": "subparagraph", "article": "10"},
        "extractor_method": "structured-parser",
    },
    "assertion": {
        "natural_language": "직통계단을 2개소 이상 설치하여야 한다.",
        "structured_payload": {"fact_path": "stair.direct_count", "operator": ">=", "value": 2},
        "interpreter_method": "human",
        "interpretation_confidence": 1.0,
        "review_status": "unreviewed",
    },
}


@pytest.mark.parametrize(("module", "service_name"), SERVICES)
def test_health_reports_its_own_service_name(module, service_name):
    response = TestClient(module.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": service_name}


@pytest.mark.parametrize(("module", "_service_name"), SERVICES)
def test_metrics_endpoint_serves_prometheus_text(module, _service_name):
    response = TestClient(module.app).get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert response.content


def test_openapi_schema_builds_for_every_service():
    for module, _service_name in SERVICES:
        schema = TestClient(module.app).get("/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["info"]["title"].startswith("ArchOntos ")


def test_ingestion_validates_legal_version_contract():
    response = TestClient(ingestion_app.app).post(
        "/v1/contracts/legal-version/validate", json=LEGAL_VERSION_PAYLOAD
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["source_key"] == "law:001823"
    # The endpoint must actually count the evidence it was handed, otherwise a
    # dropped evidence list would still report success.
    assert body["evidence_count"] == 1


def test_ingestion_contract_rejects_missing_required_field():
    payload = {**LEGAL_VERSION_PAYLOAD}
    payload.pop("jurisdiction_code")
    response = TestClient(ingestion_app.app).post(
        "/v1/contracts/legal-version/validate", json=payload
    )
    assert response.status_code == 422


def test_ingestion_contract_rejects_unknown_extractor_method():
    payload = {
        **LEGAL_VERSION_PAYLOAD,
        "evidence": [{**LEGAL_VERSION_PAYLOAD["evidence"][0], "extractor_method": "gpt-9"}],
    }
    response = TestClient(ingestion_app.app).post(
        "/v1/contracts/legal-version/validate", json=payload
    )
    assert response.status_code == 422


def test_normalization_validates_assertion_contract():
    response = TestClient(normalization_app.app).post(
        "/v1/contracts/assertion/validate", json=ASSERTION_CONTRACT_PAYLOAD
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["extractor_method"] == "structured-parser"
    assert body["interpreter_method"] == "human"
    assert body["review_status"] == "unreviewed"


def test_rule_engine_classifies_query_without_a_database():
    response = TestClient(rule_engine_app.app).get(
        "/v1/query/classify", params={"query": "서울과 부산 기준이 달라?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "서울과 부산 기준이 달라?"
    assert body["intent"]


def test_rule_engine_classify_requires_a_query():
    response = TestClient(rule_engine_app.app).get("/v1/query/classify", params={"query": ""})
    assert response.status_code == 422


def test_action_service_classifies_and_proposes():
    client = TestClient(action_app.app)

    classified = client.post("/v1/query/classify", json={"query": "이 조항 원문 보여줘"})
    assert classified.status_code == 200
    assert classified.json()["intent"]

    proposed = client.post(
        "/v1/actions/report/propose",
        json={"target_refs": ["rule:1"], "context": {"note": "smoke"}},
    )
    assert proposed.status_code == 200
    body = proposed.json()
    assert body["action_type"] == "Report"
    assert body["target_refs"] == ["rule:1"]
    # A write action stays a proposal until an approval gate is satisfied.
    assert body["proposed_output"]["status"] == "proposal"
    assert body["requires_approval"] is True


def test_action_classify_rejects_a_too_short_query():
    response = TestClient(action_app.app).post("/v1/query/classify", json={"query": "x"})
    assert response.status_code == 422


def test_projection_status_declares_rebuildable_projections():
    response = TestClient(projection_app.app).get("/v1/status")
    assert response.status_code == 200
    body = response.json()
    assert body["canonical_source"] == "postgresql"
    assert body["rebuildable"] is True
    assert body["event_transport"] == "transactional-outbox"


def test_normalization_process_one_reaches_the_database(mvp0_db, monkeypatch, tmp_path):
    """Drive a real HTTP request through to a real database round trip.

    With an empty outbox the worker has nothing to claim, so the endpoint must
    report ``processed: false`` rather than 500. This is the only assertion in
    the suite that proves the HTTP layer is actually wired to the session.
    """
    session_factory, _schema = mvp0_db

    monkeypatch.setattr(normalization_app, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(
        normalization_app, "_artifact_store", lambda: LocalArtifactStore(tmp_path / "artifacts")
    )

    response = TestClient(normalization_app.app).post("/v1/normalization/process-one")
    assert response.status_code == 200
    assert response.json() == {"processed": False}


def test_assertion_candidate_for_unknown_evidence_returns_404(mvp0_db, monkeypatch, tmp_path):
    """A missing evidence row must surface as 404, not a 500 or a silent success."""
    session_factory, _schema = mvp0_db
    from uuid import uuid4

    monkeypatch.setattr(normalization_app, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(
        normalization_app, "_artifact_store", lambda: LocalArtifactStore(tmp_path / "artifacts")
    )

    response = TestClient(normalization_app.app).post(
        "/v1/assertions/candidates",
        json={
            "evidence_span_id": str(uuid4()),
            "natural_language": "직통계단을 2개소 이상 설치하여야 한다.",
            "structured_payload": {"fact_path": "stair.direct_count", "operator": ">=", "value": 2},
            "interpreter_method": "human",
            "interpretation_confidence": 1.0,
        },
    )
    assert response.status_code == 404
