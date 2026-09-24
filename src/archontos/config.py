from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHONTOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://archontos:archontos@localhost:5432/archontos"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "archontos"
    minio_secret_key: str = "change-me"
    minio_secure: bool = False
    allowed_jurisdictions: str = Field(default="KR")
    otel_enabled: bool = False
    lawgo_oc: str | None = None
    lawgo_base_url: str = "https://www.law.go.kr/DRF"

    @property
    def jurisdiction_set(self) -> set[str]:
        return {item.strip() for item in self.allowed_jurisdictions.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
