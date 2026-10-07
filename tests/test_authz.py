import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import apps.action as action_app
import apps.ingestion as ingestion_app
import apps.normalization as normalization_app
import apps.projection as projection_app
import apps.rule_engine as rule_engine_app
from archontos.actions.persistence import MemoryActionStore
from archontos.authz import (
    ROLE_PERMISSIONS,
    AuthorizationError,
    Permission,
    authorize,
    check_four_eyes,
    parse_actor_jurisdictions,
    parse_actor_roles,
    permissions_for,
)
from archontos.config import get_settings
from archontos.identity import Principal

APPS = (action_app, ingestion_app, normalization_app, projection_app, rule_engine_app)


def test_every_v1_route_declares_a_permission():
    missing = []
    for module in APPS:
        for route in module.app.routes:
            if isinstance(route, APIRoute) and route.path.startswith("/v1/"):
                names = {dep.call.__qualname__ for dep in route.dependant.dependencies}
                if not any("require" in name for name in names):
                    missing.append(f"{module.__name__} {route.path}")
    assert missing == []


def test_parse_actor_roles_and_jurisdictions():
    assert parse_actor_roles("alice:approver+executor, bob:proposer") == {
        "alice": frozenset({"approver", "executor"}),
        "bob": frozenset({"proposer"}),
    }
    assert parse_actor_jurisdictions("alice:KR+JP") == {"alice": ("JP", "KR")}
    with pytest.raises(ValueError, match="unknown role"):
        parse_actor_roles("alice:root")
    with pytest.raises(ValueError):
        parse_actor_roles("no-colon")


def test_admin_has_every_permission_and_viewer_is_read_only():
    assert permissions_for(frozenset({"admin"})) == frozenset(Permission)
    viewer = ROLE_PERMISSIONS["viewer"]
    assert Permission.ACTION_APPROVE not in viewer
    assert Permission.QUERY_READ in viewer


def test_authorize_rules():
    authorize(None, Permission.ACTION_APPROVE, enabled=False)  # disabled: open
    with pytest.raises(AuthorizationError) as anon:
        authorize(Principal("x", False), Permission.QUERY_READ, enabled=True)
    assert anon.value.status_code == 401
    with pytest.raises(AuthorizationError) as denied:
        authorize(
            Principal("bob", True, frozenset({"proposer"})),
            Permission.ACTION_APPROVE,
            enabled=True,
        )
    assert denied.value.status_code == 403
    authorize(
        Principal("al", True, frozenset({"approver"})), Permission.ACTION_APPROVE, enabled=True
    )


def test_four_eyes():
    check_four_eyes(Principal("a", True), "b", enabled=True)
    check_four_eyes(Principal("a", True), "a", enabled=False)
    with pytest.raises(AuthorizationError):
        check_four_eyes(Principal("a", True), "a", enabled=True)


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "memory")
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "pam:kp,abe:ka,eve:ke,ann:kn,root:kr")
    monkeypatch.setenv(
        "ARCHONTOS_ACTOR_ROLES",
        "pam:proposer,abe:approver,eve:executor,root:admin",  # ann: authenticated, no role
    )
    get_settings.cache_clear()
    monkeypatch.setattr(action_app, "_memory", MemoryActionStore())
    yield TestClient(action_app.app)
    get_settings.cache_clear()


def _h(key):
    return {"x-api-key": key}


def test_role_based_action_lifecycle(secured):
    client = secured
    body = {"target_refs": ["rule:1"], "context": {}}
    assert client.post("/v1/actions/report/propose", json=body, headers=_h("ka")).status_code == 403
    assert client.post("/v1/actions/report/propose", json=body, headers=_h("kn")).status_code == 403
    created = client.post("/v1/actions/report/propose", json=body, headers=_h("kp")).json()
    path = f"/v1/actions/{created['id']}"

    assert client.post(f"{path}/approve", json={}, headers=_h("kp")).status_code == 403
    assert client.post(f"{path}/execute", json={}, headers=_h("ke")).status_code == 409
    assert client.post(f"{path}/approve", json={}, headers=_h("ke")).status_code == 403
    approved = client.post(f"{path}/approve", json={}, headers=_h("ka"))
    assert approved.status_code == 200
    assert client.post(f"{path}/execute", json={}, headers=_h("ka")).status_code == 403
    done = client.post(f"{path}/execute", json={}, headers=_h("ke"))
    assert done.json()["status"] == "succeeded"
    assert client.get(path, headers=_h("kn")).status_code == 403
    assert client.get(path, headers=_h("kp")).status_code == 200


def test_four_eyes_blocks_self_approval_even_for_admin(secured):
    client = secured
    body = {"target_refs": ["rule:1"], "context": {}}
    created = client.post("/v1/actions/report/propose", json=body, headers=_h("kr")).json()
    response = client.post(f"/v1/actions/{created['id']}/approve", json={}, headers=_h("kr"))
    assert response.status_code == 403
    assert "proposer" in response.json()["detail"]


def test_roles_without_api_keys_require_authentication(monkeypatch):
    monkeypatch.delenv("ARCHONTOS_API_KEYS", raising=False)
    monkeypatch.setenv("ARCHONTOS_ACTOR_ROLES", "dev:admin")
    get_settings.cache_clear()
    try:
        client = TestClient(action_app.app)
        response = client.post(
            "/v1/actions/report/propose",
            json={"target_refs": ["r"], "context": {}},
            headers={"x-actor": "dev"},  # a hint is not authentication
        )
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()
