from datetime import date
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

from archontos.domain.contracts import EvidenceSpanContract


class RawArtifactManifest(BaseModel):
    source_name: str
    source_url: HttpUrl
    fetched_at: str
    content_hash: str = Field(min_length=16)
    mime_type: str
    storage_uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedLegalVersion(BaseModel):
    source_key: str
    title: str
    issuer: str
    jurisdiction_code: str
    document_type: str
    version_label: str
    effective_from: date
    effective_to: date | None = None
    artifact: RawArtifactManifest
    evidence: list[EvidenceSpanContract]
