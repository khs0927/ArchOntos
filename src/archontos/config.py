from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The development placeholder. It is also what docker-compose.yml ships, and it
# is the value that must never survive into a non-dev environment.
DEV_SECRET_PLACEHOLDER = "change-me"  # noqa: S105 - documented dev value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHONTOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://archontos:archontos@localhost:5432/archontos"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "archontos"
    minio_secret_key: str = DEV_SECRET_PLACEHOLDER
    minio_secure: bool = False
    minio_bucket: str = "archontos"
    artifact_backend: str = "local"
    artifact_local_path: str = "local-data/artifacts"
    allowed_jurisdictions: str = Field(default="KR")
    otel_enabled: bool = False
    lawgo_oc: str | None = None
    lawgo_base_url: str = "https://www.law.go.kr/DRF"
    # Empty means open, matching local MVP-0. Set a comma-separated list to require X-API-Key.
    api_keys: str = ""
    # memory keeps the smoke path DB-free. postgres is the canonical action/projection backend.
    action_backend: Literal["memory", "postgres"] = "memory"
    # none keeps embedding NULL; hashing is deterministic; fastembed needs the [embeddings] extra.
    embedder: Literal["none", "hashing", "fastembed"] = "none"
    # Startup check that the service DB login cannot bypass row-level security.
    # off: skip | warn: log (default) | enforce: refuse to start (production, Helm default).
    db_privilege_check: Literal["off", "warn", "enforce"] = "warn"
    embedding_model: str | None = None
    embedding_cache_dir: str | None = None

    @model_validator(mode="after")
    def _no_placeholder_secret_outside_dev(self) -> "Settings":
        """Refuse to start a non-dev environment on the development password.

        The default made object storage silently come up on a publicly known
        secret whenever ARCHONTOS_MINIO_SECRET_KEY was unset, and nothing
        reported it. Failing at construction is the only point where the
        process is still allowed to stop.
        """
        if (
            self.env != "dev"
            and self.artifact_backend == "minio"
            and self.minio_secret_key == DEV_SECRET_PLACEHOLDER
        ):
            raise ValueError(
                "ARCHONTOS_MINIO_SECRET_KEY is still the development placeholder; "
                "refusing to start a non-dev environment with it"
            )
        return self

    @property
    def jurisdiction_set(self) -> set[str]:
        return {item.strip() for item in self.allowed_jurisdictions.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
