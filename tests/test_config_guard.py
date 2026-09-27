"""A development password must not survive into a running environment."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from archontos.config import DEV_SECRET_PLACEHOLDER, Settings


def _settings(**overrides) -> Settings:
    base = {
        "env": "dev",
        "artifact_backend": "minio",
        "minio_secret_key": DEV_SECRET_PLACEHOLDER,
    }
    base.update(overrides)
    return Settings(**base)


def test_dev_may_use_the_placeholder() -> None:
    assert _settings().minio_secret_key == DEV_SECRET_PLACEHOLDER


def test_non_dev_refuses_the_placeholder() -> None:
    """The default made object storage come up on a publicly known secret."""
    with pytest.raises(ValidationError) as caught:
        _settings(env="production")
    assert "development placeholder" in str(caught.value)


def test_non_dev_accepts_a_real_secret() -> None:
    settings = _settings(env="production", minio_secret_key="an-actual-secret")
    assert settings.minio_secret_key == "an-actual-secret"


def test_a_non_dev_environment_on_local_storage_is_unaffected() -> None:
    """Only object storage has the exposed default; do not break other deploys."""
    settings = _settings(env="production", artifact_backend="local")
    assert settings.minio_secret_key == DEV_SECRET_PLACEHOLDER
