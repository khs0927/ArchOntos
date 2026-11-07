"""OIDC/JWT identity with a locally generated RSA key (no network)."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import apps.action as action_app
import apps.common as common
from archontos.actions.persistence import MemoryActionStore
from archontos.config import Settings, get_settings
from archontos.oidc import OidcConfig, OidcError, OidcVerifier, get_verifier

ISS = "https://idp.example.test/realms/archontos"
AUD = "archontos-api"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _config(**kw) -> OidcConfig:
    return OidcConfig(issuer=ISS, audience=AUD, jwks_url="https://idp.example.test/jwks", **kw)


def _verifier(**kw) -> OidcVerifier:
    return OidcVerifier(_config(), lambda _token: _KEY.public_key(), **kw)


def _token(key=_KEY, **claims) -> str:
    now = int(time.time())
    payload = {"iss": ISS, "aud": AUD, "exp": now + 300, "sub": "alice", "roles": ["approver"]}
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k1"})


def test_valid_token_maps_actor_roles_and_jurisdictions():
    principal = _verifier().verify(_token(jurisdictions=["KR", "JP"], roles="viewer approver"))
    assert principal.actor == "alice"
    assert principal.authenticated is True
    assert principal.roles == {"viewer", "approver"}
    assert principal.jurisdictions == ("JP", "KR")


def test_unknown_roles_dropped_and_actor_roles_merged():
    verifier = _verifier(actor_roles="alice:executor", actor_jurisdictions="alice:KR")
    principal = verifier.verify(_token(roles=["root", "viewer"]))
    assert principal.roles == {"viewer", "executor"}
    assert principal.jurisdictions == ("KR",)


def test_custom_claims_and_nested_roles_path():
    cfg = _config(actor_claim="email", roles_claim="realm_access.roles")
    verifier = OidcVerifier(cfg, lambda _t: _KEY.public_key())
    token = _token(email="bob@corp.example", realm_access={"roles": ["proposer"]})
    principal = verifier.verify(token)
    assert principal.actor == "bob@corp.example"
    assert principal.roles == {"proposer"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exp": int(time.time()) - 3600},
        {"iss": "https://evil.example"},
        {"aud": "someone-else"},
        {"aud": None},
        {"exp": None},
        {"sub": "bad actor!"},
        {"sub": None},
    ],
)
def test_rejected_claims(kwargs):
    with pytest.raises(OidcError):
        _verifier().verify(_token(**kwargs))


def test_wrong_signing_key_rejected():
    with pytest.raises(OidcError):
        _verifier().verify(_token(key=_OTHER))


def test_hs256_and_garbage_rejected():
    forged = jwt.encode(
        {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 60, "sub": "x"}, "k" * 40, "HS256"
    )
    with pytest.raises(OidcError):
        _verifier().verify(forged)
    with pytest.raises(OidcError):
        _verifier().verify("not-a-jwt")


def test_disabled_by_default_and_partial_config_refused():
    assert OidcConfig.from_settings(Settings(_env_file=None)) is None
    with pytest.raises(ValueError):
        OidcConfig.from_settings(Settings(_env_file=None, oidc_issuer=ISS))
    full = Settings(
        _env_file=None, oidc_issuer=ISS, oidc_audience=AUD, oidc_jwks_url="https://x.test/jwks"
    )
    assert OidcConfig.from_settings(full) is not None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ARCHONTOS_API_KEYS", raising=False)
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "memory")
    monkeypatch.setenv("ARCHONTOS_ACTOR_ROLES", "alice:proposer")
    get_settings.cache_clear()
    monkeypatch.setattr(action_app, "_memory", MemoryActionStore())
    monkeypatch.setattr(common, "get_verifier", lambda: _verifier(actor_roles="alice:proposer"))
    yield TestClient(action_app.app)
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_BODY = {"target_refs": ["rule:1"], "context": {}}


def test_http_bearer_feeds_actor_and_roles(client):
    response = client.post(
        "/v1/actions/report/propose", json=_BODY, headers=_auth(_token(roles=["proposer"]))
    )
    assert response.status_code == 200
    assert response.headers["x-archontos-actor"] == "alice"


def test_http_role_from_token_is_enforced(client):
    response = client.post(
        "/v1/actions/report/propose",
        json=_BODY,
        headers=_auth(_token(sub="carol", roles=["viewer"])),
    )
    assert response.status_code == 403


def test_http_missing_or_bad_bearer_is_401(client):
    anon = client.post("/v1/actions/report/propose", json=_BODY)
    assert anon.status_code == 401
    assert anon.headers["www-authenticate"] == "Bearer"
    bad = client.post("/v1/actions/report/propose", json=_BODY, headers=_auth(_token(key=_OTHER)))
    assert bad.status_code == 401
    assert client.get("/health").status_code == 200


def test_http_api_key_still_works_next_to_oidc(client, monkeypatch):
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "alice:secret-key")
    get_settings.cache_clear()
    ok = client.post("/v1/actions/report/propose", json=_BODY, headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200
    nope = client.post("/v1/actions/report/propose", json=_BODY, headers={"X-API-Key": "nope"})
    assert nope.status_code == 401


def test_get_verifier_none_when_disabled(monkeypatch):
    for name in ("ISSUER", "AUDIENCE", "JWKS_URL"):
        monkeypatch.delenv(f"ARCHONTOS_OIDC_{name}", raising=False)
    get_settings.cache_clear()
    assert get_verifier() is None
    get_settings.cache_clear()
