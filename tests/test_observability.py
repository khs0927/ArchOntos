"""Structured logs, request ids, Prometheus metrics and audit events (no DB needed)."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import apps.action as action_app
from archontos.actions.persistence import MemoryActionStore
from archontos.config import get_settings
from archontos.observability import (
    JsonFormatter,
    configure_logging,
    sanitize_request_id,
    status_class,
)

SECRET_KEY = "k-super-secret-value"
BODY = {"target_refs": ["rule:1"], "context": {}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "memory")
    monkeypatch.setenv("ARCHONTOS_API_KEYS", f"pam:{SECRET_KEY},abe:k-approver-1234")
    monkeypatch.setenv("ARCHONTOS_ACTOR_ROLES", "pam:proposer,abe:proposer+approver")
    get_settings.cache_clear()
    monkeypatch.setattr(action_app, "_memory", MemoryActionStore())
    yield TestClient(action_app.app)
    get_settings.cache_clear()


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _records(caplog, logger: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger]


def test_request_log_fields_and_route_template(client, caplog):
    caplog.set_level(logging.INFO)
    created = client.post(
        "/v1/actions/report/propose", json=BODY, headers={"x-api-key": SECRET_KEY}
    ).json()
    client.get(f"/v1/actions/{created['id']}", headers={"x-api-key": SECRET_KEY})
    recs = _records(caplog, "archontos.request")
    fields = recs[-1].fields  # type: ignore[attr-defined]
    assert fields["route"] == "/v1/actions/{action_id}"
    assert created["id"] not in json.dumps(fields)
    assert fields["method"] == "GET"
    assert fields["status"] == 200
    assert fields["actor"] == "pam"
    assert fields["authenticated"] is True
    assert fields["duration_ms"] >= 0
    assert len(fields["request_id"]) >= 8


def test_json_formatter_shape(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/health")
    record = _records(caplog, "archontos.request")[-1]
    line = json.loads(JsonFormatter().format(record))
    assert set(line) >= {
        "ts",
        "level",
        "logger",
        "message",
        "request_id",
        "method",
        "route",
        "status",
        "duration_ms",
        "actor",
        "authenticated",
    }
    assert line["route"] == "/health"


def test_configure_logging_is_idempotent_and_json(capsys):
    configure_logging("json", "INFO")
    configure_logging("json", "INFO")
    logger = logging.getLogger("archontos")
    assert len([h for h in logger.handlers if getattr(h, "_archontos_handler", False)]) == 1
    logging.getLogger("archontos.request").info("request", extra={"fields": {"route": "/x"}})
    err = capsys.readouterr().err.strip().splitlines()
    assert json.loads(err[-1])["route"] == "/x"
    configure_logging("text", "INFO")


def test_no_secret_leakage(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.get(
        "/v1/actions/nope", headers={"x-api-key": SECRET_KEY, "authorization": "Bearer tok-abc"}
    )
    client.get("/v1/actions/nope", headers={"x-api-key": "wrong-key-value"})
    client.get("/v1/actions/nope", headers={"authorization": "Bearer tok-abc"})
    rendered = "\n".join(JsonFormatter().format(r) + r.getMessage() for r in caplog.records)
    for secret in (SECRET_KEY, "wrong-key-value", "tok-abc"):
        assert secret not in rendered


def test_forbidden_fields_are_dropped():
    record = logging.LogRecord("archontos.x", logging.INFO, "f", 1, "m", None, None)
    record.fields = {"authorization": "Bearer zzz", "route": "/r"}  # type: ignore[attr-defined]
    out = json.loads(JsonFormatter().format(record))
    assert "authorization" not in out and out["route"] == "/r"


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("req-12345678", True),
        ("A" * 64, True),
        ("short", False),
        ("A" * 65, False),
        ("bad id with spaces", False),
        ("evil\nINJECT", False),
        ('{"a":"b"}12345', False),
        (None, False),
    ],
)
def test_sanitize_request_id(raw, kept):
    out = sanitize_request_id(raw)
    assert (out == raw) is kept
    assert out.replace("-", "").isalnum() or out == raw


def test_request_id_propagates_and_is_replaced(client, caplog):
    caplog.set_level(logging.INFO)
    good = client.get("/health", headers={"x-request-id": "trace-abcdef12"})
    assert good.headers["x-request-id"] == "trace-abcdef12"
    bad = client.get("/health", headers={"x-request-id": "bad id!!"})
    assert bad.headers["x-request-id"] != "bad id!!"
    assert len(bad.headers["x-request-id"]) == 32


def test_metrics_increment_with_bounded_labels(client):
    labels = {
        "service": "action",
        "route": "/v1/actions/{action_id}",
        "method": "GET",
        "status_class": "4xx",
    }
    before = _sample("archontos_http_request_duration_seconds_count", **labels)
    for i in range(3):  # distinct raw paths, one template
        client.get(f"/v1/actions/id-{i}", headers={"x-api-key": SECRET_KEY})
    # 404 from the handler (unknown id) or 403 (no action.read)? either way the class is 4xx.
    assert _sample("archontos_http_request_duration_seconds_count", **labels) == before + 3
    seen = {
        s.labels["route"]
        for m in REGISTRY.collect()
        if m.name == "archontos_http_request_duration_seconds"
        for s in m.samples
    }
    assert not any("id-" in route for route in seen)
    assert _sample("archontos_http_requests_in_flight", service="action") == 0


def test_unknown_path_and_method_are_bounded(client):
    client.request("BREW", "/no/such/path/123", headers={"x-api-key": SECRET_KEY})
    assert (
        _sample(
            "archontos_http_request_duration_seconds_count",
            service="action",
            route="unmatched",
            method="OTHER",
            status_class="4xx",
        )
        >= 1
    )
    assert status_class(404) == "4xx" and status_class(999) == "other"


def test_auth_failure_metrics_and_audit(client, caplog):
    caplog.set_level(logging.INFO)
    base = {
        m: _sample("archontos_auth_failures_total", service="action", reason=m)
        for m in ("missing_key", "invalid_key")
    }
    client.get("/v1/actions/x")
    client.get("/v1/actions/x", headers={"x-api-key": "nope"})
    assert _sample("archontos_auth_failures_total", service="action", reason="missing_key") == (
        base["missing_key"] + 1
    )
    assert _sample("archontos_auth_failures_total", service="action", reason="invalid_key") == (
        base["invalid_key"] + 1
    )
    audits = [r.fields for r in _records(caplog, "archontos.audit")]  # type: ignore[attr-defined]
    assert {a["event"] for a in audits} == {"authentication_failed"}
    assert all(a["outcome"] == "denied" and a["actor"] == "anonymous" for a in audits)


def test_forbidden_and_four_eyes_audit(client, caplog):
    caplog.set_level(logging.INFO)
    forbidden_before = _sample(
        "archontos_auth_failures_total", service="action", reason="forbidden"
    )
    four_before = _sample("archontos_auth_failures_total", service="action", reason="four_eyes")
    # pam is a proposer only: approving is forbidden.
    created = client.post(
        "/v1/actions/report/propose", json=BODY, headers={"x-api-key": "k-approver-1234"}
    ).json()
    path = f"/v1/actions/{created['id']}/approve"
    assert client.post(path, json={}, headers={"x-api-key": SECRET_KEY}).status_code == 403
    # abe proposed it and also holds approver: four-eyes blocks.
    assert client.post(path, json={}, headers={"x-api-key": "k-approver-1234"}).status_code == 403
    assert (
        _sample("archontos_auth_failures_total", service="action", reason="forbidden")
        == forbidden_before + 1
    )
    assert (
        _sample("archontos_auth_failures_total", service="action", reason="four_eyes")
        == four_before + 1
    )
    audits = [r.fields for r in _records(caplog, "archontos.audit")]  # type: ignore[attr-defined]
    denied = next(a for a in audits if a["event"] == "authorization_denied")
    assert denied["actor"] == "pam"
    assert denied["permission"] == "action.approve"
    assert denied["route"] == "/v1/actions/{action_id}/approve"
    assert denied["request_id"]
    eyes = next(a for a in audits if a["event"] == "four_eyes_violation")
    assert eyes["actor"] == "abe" and eyes["outcome"] == "denied"
    assert created["id"] not in json.dumps(audits)


def test_log_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARCHONTOS_LOG_FORMAT", raising=False)
    monkeypatch.delenv("ARCHONTOS_LOG_LEVEL", raising=False)
    s = get_settings.__wrapped__()
    assert s.log_format == "text" and s.log_level == "INFO"
