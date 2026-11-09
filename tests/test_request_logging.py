"""Structured JSON request/audit logging."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

import apps.action as action_app
from archontos.actions.persistence import MemoryActionStore
from archontos.config import get_settings
from archontos.observability import REQUEST_LOGGER, JsonFormatter, configure_logging


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "memory")
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "alice:k1")
    monkeypatch.delenv("ARCHONTOS_OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(action_app, "_memory", MemoryActionStore())
    yield TestClient(action_app.app)
    get_settings.cache_clear()
    configure_logging("off")


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _capture() -> io.StringIO:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger(REQUEST_LOGGER).addHandler(handler)
    return stream


def test_request_is_logged_as_json_with_actor_and_request_id(client):
    stream = _capture()
    response = client.post(
        "/v1/actions/report/propose",
        json={"target_refs": ["rule:1"], "context": {}},
        headers={"X-API-Key": "k1", "X-Request-Id": "req-123"},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-123"
    record = next(r for r in _lines(stream) if r["event"] == "http_request")
    assert record["actor"] == "alice"
    assert record["status"] == 200
    assert record["method"] == "POST"
    assert record["request_id"] == "req-123"
    assert record["route"] == "/v1/actions/report/propose"
    assert "k1" not in json.dumps(record)


def test_unauthorized_request_is_logged_and_bad_request_id_replaced(client):
    stream = _capture()
    response = client.get("/v1/actions/xyz", headers={"X-Request-Id": "bad id"})
    assert response.status_code == 401
    record = next(r for r in _lines(stream) if r["event"] == "http_request")
    assert record["status"] == 401
    assert record["actor"] == "anonymous"
    assert record["request_id"] == response.headers["x-request-id"] != "bad id"


def test_health_is_not_logged(client):
    stream = _capture()
    client.get("/health")
    assert _lines(stream) == []


def test_formatter_includes_extra_fields():
    record = logging.LogRecord("n", logging.INFO, "f", 1, "hi %s", ("x",), None)
    record.custom = 7
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hi x"
    assert out["custom"] == 7
    assert out["level"] == "INFO"
