from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx


class SourceAdapterError(RuntimeError):
    """Base error for external official-source adapters."""


class SourceAuthenticationError(SourceAdapterError):
    """Raised when a source adapter is missing required credentials."""


class SourceProtocolError(SourceAdapterError):
    """Raised when an official source returns an unexpected payload."""


def _yyyymmdd(value: str | int | None) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) != 8 or not text.isdigit():
        raise SourceProtocolError(f"Invalid YYYYMMDD date from source: {text!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _content(value: Any) -> Any:
    if isinstance(value, dict) and "content" in value:
        return value["content"]
    return value


@dataclass(frozen=True, slots=True)
class RawSourceEnvelope:
    source_name: str
    endpoint: str
    params: dict[str, Any]
    payload: dict[str, Any]
    fetched_at: datetime

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class LawSearchItem:
    law_name: str
    law_id: str
    mst: str
    law_type: str
    ministry: str
    promulgation_date: date | None
    promulgation_number: str
    enforcement_date: date | None
    revision_type: str
    history_code: str
    detail_link: str


@dataclass(frozen=True, slots=True)
class LawSearchPage:
    target: str
    query: str
    page: int
    total_count: int
    items: tuple[LawSearchItem, ...]
    envelope: RawSourceEnvelope


@dataclass(frozen=True, slots=True)
class LawBody:
    law_name: str
    law_id: str
    mst: str | None
    # Identity as declared by the response payload only, with no fallback to
    # the request parameters. `law_id` and `mst` above fall back to the request
    # echo, so comparing them against the request proves nothing. These two are
    # what a cross-check must use, and None means the provider did not declare
    # the value, which is unverifiable rather than matching.
    declared_law_id: str | None
    declared_mst: str | None
    law_type: str
    ministry: str
    promulgation_date: date | None
    enforcement_date: date | None
    articles: tuple[dict[str, Any], ...]
    addenda: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]
    envelope: RawSourceEnvelope


class LawGoKrAdapter:
    """Read-only client for the Korean National Law Information Center DRF API."""

    source_name = "law.go.kr"

    def __init__(
        self,
        base_url: str = "https://www.law.go.kr/DRF",
        oc: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.oc = (oc or "").strip()
        self._client = client
        self.timeout_seconds = timeout_seconds

    def _require_auth(self) -> None:
        if not self.oc:
            raise SourceAuthenticationError("law.go.kr Open API OC value is required")

    async def _request_json(self, path: str, params: dict[str, Any]) -> RawSourceEnvelope:
        self._require_auth()
        request_params = {"OC": self.oc, "type": "JSON", **params}
        endpoint = f"{self.base_url}/{path.lstrip('/')}"
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.get(endpoint, params=request_params)
            response.raise_for_status()
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                preview = response.text[:160].replace(self.oc, "***")
                raise SourceProtocolError(
                    f"law.go.kr returned non-JSON data for {path}: {preview!r}"
                ) from exc
            if not isinstance(payload, dict):
                raise SourceProtocolError(
                    f"law.go.kr returned {type(payload).__name__}; expected a JSON object"
                )
            safe_params = {key: value for key, value in request_params.items() if key != "OC"}
            return RawSourceEnvelope(
                source_name=self.source_name,
                endpoint=endpoint,
                params=safe_params,
                payload=payload,
                fetched_at=datetime.now(UTC),
            )
        except httpx.HTTPStatusError as exc:
            raise SourceProtocolError(
                f"law.go.kr HTTP error {exc.response.status_code} for {path}"
            ) from exc
        finally:
            if owned_client:
                await client.aclose()

    @staticmethod
    def parse_search(envelope: RawSourceEnvelope) -> LawSearchPage:
        root = envelope.payload.get("LawSearch") or envelope.payload.get("lawSearch")
        if not isinstance(root, dict):
            raise SourceProtocolError("law.go.kr search payload is missing LawSearch")

        items: list[LawSearchItem] = []
        for item in _as_list(root.get("law")):
            if not isinstance(item, dict):
                continue
            items.append(
                LawSearchItem(
                    law_name=str(item.get("법령명한글") or ""),
                    law_id=str(item.get("법령ID") or ""),
                    mst=str(item.get("법령일련번호") or ""),
                    law_type=str(item.get("법령구분명") or ""),
                    ministry=str(item.get("소관부처명") or ""),
                    promulgation_date=_yyyymmdd(item.get("공포일자")),
                    promulgation_number=str(item.get("공포번호") or ""),
                    enforcement_date=_yyyymmdd(item.get("시행일자")),
                    revision_type=str(item.get("제개정구분명") or ""),
                    history_code=str(item.get("현행연혁코드") or ""),
                    detail_link=str(item.get("법령상세링크") or ""),
                )
            )

        return LawSearchPage(
            target=str(root.get("target") or envelope.params.get("target") or ""),
            query=str(root.get("키워드") or envelope.params.get("query") or ""),
            page=int(root.get("page") or 1),
            total_count=int(root.get("totalCnt") or len(items)),
            items=tuple(items),
            envelope=envelope,
        )

    @staticmethod
    def parse_body(envelope: RawSourceEnvelope) -> LawBody:
        law = envelope.payload.get("법령")
        if not isinstance(law, dict):
            raise SourceProtocolError("law.go.kr body payload is missing 법령")
        info = law.get("기본정보") or {}
        if not isinstance(info, dict):
            info = {}

        article_root = law.get("조문") or {}
        article_units = article_root.get("조문단위") if isinstance(article_root, dict) else None
        addenda_root = law.get("부칙") or {}
        addenda_units = addenda_root.get("부칙단위") if isinstance(addenda_root, dict) else None
        attachment_root = law.get("별표") or {}
        attachment_units = (
            attachment_root.get("별표단위") if isinstance(attachment_root, dict) else None
        )

        # Request selectors are not evidence of the identity actually returned.
        mst = info.get("법령일련번호") or law.get("법령키")
        law_id = info.get("법령ID")
        for selector, observed in (("MST", mst), ("ID", law_id)):
            requested = envelope.params.get(selector)
            if requested and observed in (None, ""):
                raise SourceProtocolError(f"law.go.kr response is missing requested {selector}")
            if requested and str(requested) != str(observed):
                raise SourceProtocolError(
                    f"law.go.kr returned a different {selector} than requested"
                )
        declared_law_id = law_id
        declared_mst = mst
        return LawBody(
            law_name=str(info.get("법령명_한글") or info.get("법령명한글") or ""),
            law_id=str(law_id or ""),
            mst=str(mst) if mst not in (None, "") else None,
            declared_law_id=str(declared_law_id) if declared_law_id not in (None, "") else None,
            declared_mst=str(declared_mst) if declared_mst not in (None, "") else None,
            law_type=str(_content(info.get("법종구분")) or info.get("법령구분명") or ""),
            ministry=str(_content(info.get("소관부처")) or info.get("소관부처명") or ""),
            promulgation_date=_yyyymmdd(info.get("공포일자")),
            enforcement_date=_yyyymmdd(info.get("시행일자")),
            articles=tuple(item for item in _as_list(article_units) if isinstance(item, dict)),
            addenda=tuple(item for item in _as_list(addenda_units) if isinstance(item, dict)),
            attachments=tuple(
                item for item in _as_list(attachment_units) if isinstance(item, dict)
            ),
            envelope=envelope,
        )

    async def search_laws(
        self,
        query: str,
        *,
        page: int = 1,
        display: int = 20,
        search: int = 1,
        sort: str = "lasc",
    ) -> LawSearchPage:
        envelope = await self._request_json(
            "lawSearch.do",
            {
                "target": "law",
                "query": query,
                "page": page,
                "display": min(max(display, 1), 100),
                "search": search,
                "sort": sort,
            },
        )
        return self.parse_search(envelope)

    async def search_effective_versions(
        self,
        query: str,
        *,
        page: int = 1,
        display: int = 100,
        status: str = "1,2,3",
        sort: str = "efdes",
    ) -> LawSearchPage:
        envelope = await self._request_json(
            "lawSearch.do",
            {
                "target": "eflaw",
                "query": query,
                "page": page,
                "display": min(max(display, 1), 100),
                "nw": status,
                "sort": sort,
            },
        )
        return self.parse_search(envelope)

    async def fetch_law(
        self,
        *,
        law_id: str | None = None,
        mst: str | None = None,
        article: str | None = None,
    ) -> LawBody:
        if not law_id and not mst:
            raise ValueError("law_id or mst is required")
        params: dict[str, Any] = {"target": "law"}
        if law_id:
            params["ID"] = law_id
        if mst:
            params["MST"] = mst
        if article:
            params["JO"] = article
        return self.parse_body(await self._request_json("lawService.do", params))

    async def fetch_effective_law(
        self,
        *,
        mst: str,
        effective_date: date,
        article: str | None = None,
    ) -> LawBody:
        params: dict[str, Any] = {
            "target": "eflaw",
            "MST": mst,
            "efYd": effective_date.strftime("%Y%m%d"),
        }
        if article:
            params["JO"] = article
        return self.parse_body(await self._request_json("lawService.do", params))
