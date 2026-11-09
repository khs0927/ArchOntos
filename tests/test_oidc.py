import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from archontos import oidc
from archontos.config import get_settings
from archontos.oidc import OidcError, OidcVerifier, bearer_token

ISSUER = "https://idp.example/realms/archontos"
AUDIENCE = "archontos-api"


def _keypair(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private, jwk


PRIVATE, JWK = _keypair("k1")
OTHER_PRIVATE, _ = _keypair("k1")  # same kid, different key material


def _token(key=PRIVATE, kid="k1", alg="RS256", **overrides):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "preferred_username": "alice",
        "roles": ["approver", "not-a-role"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class _Fetcher:
    def __init__(self, keys):
        self.keys = keys
        self.calls = 0

    async def __call__(self, _url):
        self.calls += 1
        return {"keys": self.keys}


def _verifier(fetcher=None, **kw):
    return OidcVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=ISSUER + "/jwks",
        fetcher=fetcher or _Fetcher([JWK]),
        **kw,
    )


def _verify(verifier, token):
    return asyncio.run(verifier.verify(token))


def test_valid_token_becomes_principal_with_known_roles_only():
    principal = _verify(_verifier(), _token())
    assert principal.actor == "alice" and principal.authenticated
    assert principal.roles == frozenset({"approver"})
    assert principal.jurisdictions is None


def test_jurisdictions_claim_is_optional():
    v = _verifier(jurisdictions_claim="juris")
    assert _verify(v, _token(juris=["KR", "JP"])).jurisdictions == ("JP", "KR")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
        {"exp": None},
        {"preferred_username": "bad actor!"},
    ],
)
def test_rejects_bad_claims(overrides):
    with pytest.raises(OidcError):
        _verify(_verifier(), _token(**overrides))


def test_rejects_wrong_signature_and_unknown_kid():
    with pytest.raises(OidcError):
        _verify(_verifier(), _token(key=OTHER_PRIVATE))
    with pytest.raises(OidcError):
        _verify(_verifier(), _token(kid="missing"))


def test_rejects_alg_none_and_hmac_confusion():
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60}, None, "none", {"kid": "k1"}
    )
    with pytest.raises(OidcError):
        _verify(_verifier(), forged)
    hs = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60},
        "shared-secret-of-sufficient-length-32b",
        "HS256",
        {"kid": "k1"},
    )
    with pytest.raises(OidcError):
        _verify(_verifier(), hs)


def test_symmetric_algorithms_cannot_be_configured():
    with pytest.raises(ValueError):
        _verifier(algorithms=("HS256",))


def test_jwks_is_cached_and_unknown_kid_refetch_is_rate_limited():
    fetcher = _Fetcher([JWK])
    v = _verifier(fetcher)
    _verify(v, _token())
    _verify(v, _token())
    assert fetcher.calls == 1
    for _ in range(3):
        with pytest.raises(OidcError):
            _verify(v, _token(kid="nope"))
    assert fetcher.calls == 1  # refetch suppressed inside the minimum interval


def test_idp_outage_fails_closed():
    async def boom(_url):
        raise OidcError("down")

    with pytest.raises(OidcError):
        _verify(_verifier(boom), _token())


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer ") is None
    assert bearer_token("") is None


@pytest.fixture
def oidc_app(monkeypatch):
    monkeypatch.setenv("ARCHONTOS_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("ARCHONTOS_OIDC_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()
    monkeypatch.setattr(oidc, "_verifier", None)
    monkeypatch.setattr(oidc, "_http_fetch_jwks", _Fetcher([JWK]))
    from apps.common import create_service  # noqa: PLC0415 - after env change

    app = create_service("test")

    @app.get("/who")
    async def who():
        from archontos.identity import current_principal  # noqa: PLC0415

        p = current_principal()
        return {"actor": p.actor, "roles": sorted(p.roles)}

    yield TestClient(app)
    get_settings.cache_clear()
    monkeypatch.setattr(oidc, "_verifier", None)


def test_http_bearer_roundtrip_and_failures(oidc_app):
    assert oidc_app.get("/health").status_code == 200
    ok = oidc_app.get("/who", headers={"authorization": f"Bearer {_token()}"})
    assert ok.status_code == 200 and ok.json() == {"actor": "alice", "roles": ["approver"]}
    assert oidc_app.get("/who").status_code == 401
    bad = oidc_app.get("/who", headers={"authorization": "Bearer garbage"})
    assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
    # X-Actor hints are not accepted once OIDC is the authentication method
    assert oidc_app.get("/who", headers={"x-actor": "mallory"}).status_code == 401


def test_invalid_bearer_is_counted_and_audited(oidc_app, caplog):
    from archontos.observability import AUTH_FAILURES  # noqa: PLC0415

    before = AUTH_FAILURES.labels(service="test", reason="invalid_bearer")._value.get()
    with caplog.at_level("WARNING", logger="archontos.audit"):
        oidc_app.get("/who", headers={"authorization": "Bearer garbage"})
    after = AUTH_FAILURES.labels(service="test", reason="invalid_bearer")._value.get()
    assert after == before + 1
    assert "garbage" not in caplog.text
