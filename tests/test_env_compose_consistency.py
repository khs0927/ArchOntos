"""Keep .env.example and docker-compose in step with archontos.config.Settings."""

import re
from pathlib import Path

import pytest

from archontos.config import Settings

yaml = pytest.importorskip("yaml")  # ships with uvicorn[standard]

ROOT = Path(__file__).resolve().parents[1]


def _env_example() -> dict[str, str]:
    pairs = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


def _default(expr: str) -> str:
    match = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", str(expr))
    assert match, f"expected ${{VAR:-default}}, got {expr!r}"
    return match.group(2)


def test_env_example_lists_every_setting():
    env = _env_example()
    expected = {"ARCHONTOS_" + name.upper() for name in Settings.model_fields}
    assert expected <= set(env), sorted(expected - set(env))


def test_minio_credentials_match_app_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # ignore a developer's local .env
    for key in list(_env_example()):
        monkeypatch.delenv(key, raising=False)
    defaults = Settings()
    env = _env_example()
    minio = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"][
        "minio"
    ]
    password = minio["environment"]["MINIO_ROOT_PASSWORD"]
    user = minio["environment"]["MINIO_ROOT_USER"]
    assert _default(password) == defaults.minio_secret_key == env["ARCHONTOS_MINIO_SECRET_KEY"]
    assert _default(user) == defaults.minio_access_key == env["ARCHONTOS_MINIO_ACCESS_KEY"]
    assert len(defaults.minio_secret_key) >= 8  # MinIO refuses shorter root passwords


def test_infrastructure_ports_are_loopback_and_images_pinned():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for name in ("postgres", "minio"):
        for port in services[name]["ports"]:
            assert str(port).startswith("127.0.0.1:"), (name, port)
        image = services[name]["image"]
        assert ":" in image and not image.endswith(":latest"), image


def test_app_services_share_minio_credentials_and_bind_loopback():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    minio_env = services["minio"]["environment"]
    assert services["minio"].get("profiles") == ["s3"]  # opt-in, see docs/ARTIFACT-STORAGE.md
    apps = [name for name, svc in services.items() if svc.get("build")]
    assert apps, "expected application services"
    for name in apps:
        svc = services[name]
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
        env = svc.get("environment") or {}
        if "ARCHONTOS_MINIO_SECRET_KEY" in env:
            assert env["ARCHONTOS_MINIO_SECRET_KEY"] == minio_env["MINIO_ROOT_PASSWORD"], name
            assert env["ARCHONTOS_MINIO_ACCESS_KEY"] == minio_env["MINIO_ROOT_USER"], name
        if "ARCHONTOS_ARTIFACT_BACKEND" in env:
            assert _default(env["ARCHONTOS_ARTIFACT_BACKEND"]) == "local", name
