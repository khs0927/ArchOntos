from abc import ABC, abstractmethod

from archontos.ingestion.contracts import NormalizedLegalVersion


class SourceAdapter(ABC):
    """Boundary for official-source adapters.

    Source-specific HTTP/API details stay outside the canonical domain model.
    """

    @abstractmethod
    async def fetch_versions(self) -> list[NormalizedLegalVersion]:
        raise NotImplementedError


class LawGoKrAdapter(SourceAdapter):
    """Adapter contract for the Korean National Law Information Center.

    Network/auth details are intentionally not hard-coded. The first ingestion ticket will bind the
    official API/structured source and persist raw artifacts before normalization.
    """

    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def fetch_versions(self) -> list[NormalizedLegalVersion]:
        raise NotImplementedError("Bind official law.go.kr endpoint in MVP-0 ingestion ticket")
