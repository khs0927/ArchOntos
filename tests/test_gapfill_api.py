"""HTTP-level checks for the gap-fill surface on the DB-free memory backend."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import apps.action as action_app
import apps.projection as projection_app
from apps.common import key_matches
from archontos.actions.persistence import MemoryActionStore
from archontos.config import get_settings
from archontos.graph.hyperedges import MemoryHyperedgeStore


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ARCHONTOS_API_KEYS", raising=False)
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "memory")
    get_settings.cache_clear()
    monkeypatch.setattr(action_app, "_memory", MemoryActionStore())
    monkeypatch.setattr(projection_app, "_hyperedges", MemoryHyperedgeStore())
    yield
    get_settings.cache_clear()


def _propose(client: TestClient) -> dict:
    response = client.post(
        "/v1/actions/report/propose", json={"target_refs": ["rule:1"], "context": {}}
    )
    assert response.status_code == 200
    return response.json()


def test_action_lifecycle_over_http():
    client = TestClient(action_app.app)
    proposed = _propose(client)
    assert proposed["status"] == "proposed"
    assert proposed["requires_approval"] is True

    blocked = client.post(f"/v1/actions/{proposed['id']}/execute", json={"actor": "a"})
    assert blocked.status_code == 409

    approved = client.post(f"/v1/actions/{proposed['id']}/approve", json={"actor": "a"})
    assert approved.json()["status"] == "approved"
    done = client.post(f"/v1/actions/{proposed['id']}/execute", json={"actor": "a"})
    assert done.json()["status"] == "succeeded"

    again = client.post(f"/v1/actions/{proposed['id']}/execute", json={"actor": "a"})
    assert again.status_code == 409
    assert client.get(f"/v1/actions/{proposed['id']}").json()["status"] == "succeeded"


def test_rejected_action_cannot_run_or_be_approved():
    client = TestClient(action_app.app)
    proposed = _propose(client)
    rejected = client.post(
        f"/v1/actions/{proposed['id']}/reject", json={"actor": "a", "reason": "no"}
    )
    assert rejected.json()["status"] == "rejected"
    assert (
        client.post(f"/v1/actions/{proposed['id']}/approve", json={"actor": "a"}).status_code == 409
    )
    assert (
        client.post(f"/v1/actions/{proposed['id']}/execute", json={"actor": "a"}).status_code == 409
    )


def test_unknown_action_is_404_and_empty_actor_is_422():
    client = TestClient(action_app.app)
    assert client.get("/v1/actions/does-not-exist").status_code == 404
    assert client.post("/v1/actions/nope/approve", json={"actor": "a"}).status_code == 404
    proposed = _propose(client)
    assert (
        client.post(f"/v1/actions/{proposed['id']}/approve", json={"actor": ""}).status_code == 422
    )


def test_api_key_gate(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "k1, k2")
    get_settings.cache_clear()
    client = TestClient(action_app.app)
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
    body = {"target_refs": ["rule:1"], "context": {}}
    assert client.post("/v1/actions/report/propose", json=body).status_code == 401
    wrong = client.post("/v1/actions/report/propose", json=body, headers={"x-api-key": "k3"})
    assert wrong.status_code == 401
    ok = client.post("/v1/actions/report/propose", json=body, headers={"x-api-key": "k2"})
    assert ok.status_code == 200


def test_key_matches():
    assert key_matches("b", ["a", "b"]) is True
    assert key_matches("", ["a"]) is False
    assert key_matches("ab", ["a"]) is False


def test_metrics_use_route_templates_not_raw_ids():
    client = TestClient(action_app.app)
    proposed = _propose(client)
    client.get(f"/v1/actions/{proposed['id']}")
    metrics = client.get("/metrics").text
    assert 'route="/v1/actions/{action_id}"' in metrics
    assert proposed["id"] not in metrics


def test_hyperedge_create_get_and_validation():
    client = TestClient(projection_app.app)
    created = client.post(
        "/v1/hyperedges",
        json={
            "hyperedge_type": "applies-to",
            "members": [
                {"role": "rule", "ref_type": "rule", "ref_id": "r1"},
                {"role": "object", "ref_type": "space", "ref_id": "s1", "ordinal": 1},
            ],
        },
    )
    assert created.status_code == 200
    loaded = client.get(f"/v1/hyperedges/{created.json()['id']}")
    assert [m["role"] for m in loaded.json()["members"]] == ["rule", "object"]
    assert client.get("/v1/hyperedges/missing").status_code == 404
    bad = client.post("/v1/hyperedges", json={"hyperedge_type": "x", "members": [{"role": "rule"}]})
    assert bad.status_code == 422


def test_projection_drain_requires_postgres_backend():
    client = TestClient(projection_app.app)
    assert client.post("/v1/projections/drain").status_code == 409
    assert client.get("/v1/status").json()["checkpoint"] is None


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "mongo")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()


def test_identity_from_named_key_and_header(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "alice:ka")
    get_settings.cache_clear()
    client = TestClient(action_app.app)
    body = {"target_refs": ["rule:1"], "context": {}}
    created = client.post("/v1/actions/report/propose", json=body, headers={"x-api-key": "ka"})
    assert created.json()["created_by"] == "alice"
    assert created.headers["x-archontos-actor"] == "alice"
    spoof = client.post(
        f"/v1/actions/{created.json()['id']}/approve",
        json={"actor": "bob"},
        headers={"x-api-key": "ka"},
    )
    assert spoof.status_code == 403


def test_open_mode_uses_actor_header_or_anonymous():
    client = TestClient(action_app.app)
    body = {"target_refs": ["rule:1"], "context": {}}
    hinted = client.post("/v1/actions/report/propose", json=body, headers={"x-actor": "dev"})
    assert hinted.json()["created_by"] == "dev"
    bad = client.post("/v1/actions/report/propose", json=body, headers={"x-actor": "a b"})
    assert bad.json()["created_by"] == "anonymous"
    invalid = client.post(f"/v1/actions/{hinted.json()['id']}/approve", json={"actor": "no spaces"})
    assert invalid.status_code == 422


def test_similarity_search_needs_postgres_backend():
    client = TestClient(projection_app.app)
    assert client.post("/v1/search/similar", json={"query": "x"}).status_code == 409
