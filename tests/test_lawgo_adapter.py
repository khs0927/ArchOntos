import asyncio
from datetime import date

import httpx
import pytest

from archontos.ingestion.adapters import (
    LawGoKrAdapter,
    SourceAuthenticationError,
)

SEARCH_PAYLOAD = {
    "LawSearch": {
        "target": "law",
        "키워드": "건축법",
        "totalCnt": "1",
        "page": "1",
        "law": {
            "법령명한글": "건축법",
            "법령ID": "001823",
            "법령일련번호": "999001",
            "법령구분명": "법률",
            "소관부처명": "국토교통부",
            "공포일자": "20260101",
            "공포번호": "21000",
            "시행일자": "20260701",
            "제개정구분명": "일부개정",
            "현행연혁코드": "현행",
            "법령상세링크": "/DRF/lawService.do?target=law&MST=999001",
        },
    }
}

BODY_PAYLOAD = {
    "법령": {
        "법령키": "999001",
        "기본정보": {
            "법령명_한글": "건축법",
            "법령ID": "001823",
            "법령일련번호": "999001",
            "법종구분": {"content": "법률"},
            "소관부처": {"content": "국토교통부"},
            "공포일자": "20260101",
            "시행일자": "20260701",
        },
        "조문": {
            "조문단위": [
                {
                    "조문번호": "1",
                    "조문제목": "목적",
                    "조문내용": "이 법은 ...",
                }
            ]
        },
        "부칙": {"부칙단위": {"부칙공포일자": "20260101"}},
        "별표": {
            "별표단위": {
                "별표번호": "1",
                "별표제목": "용도별 건축물의 종류",
                "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=123",
            }
        },
    }
}


def _mock_client(payload):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["OC"] == "secret-oc"
        return httpx.Response(200, json=payload, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_missing_oc_fails_before_network():
    adapter = LawGoKrAdapter(oc=None)
    with pytest.raises(SourceAuthenticationError):
        asyncio.run(adapter.search_laws("건축법"))


def test_search_parses_official_fields_and_scrubs_oc():
    async def run():
        async with _mock_client(SEARCH_PAYLOAD) as client:
            adapter = LawGoKrAdapter(oc="secret-oc", client=client)
            page = await adapter.search_laws("건축법")
            assert page.total_count == 1
            assert page.items[0].law_name == "건축법"
            assert page.items[0].law_id == "001823"
            assert page.items[0].enforcement_date == date(2026, 7, 1)
            assert "OC" not in page.envelope.params
            assert "secret-oc" not in str(page.envelope.params)
            assert len(page.envelope.sha256) == 64

    asyncio.run(run())


def test_effective_version_search_uses_eflaw_target():
    async def run():
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.url.params)
            return httpx.Response(200, json=SEARCH_PAYLOAD, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = LawGoKrAdapter(oc="secret-oc", client=client)
            await adapter.search_effective_versions("건축법")
        assert seen["target"] == "eflaw"
        assert seen["nw"] == "1,2,3"
        assert seen["sort"] == "efdes"

    asyncio.run(run())


def test_body_parser_preserves_articles_addenda_and_attachments():
    async def run():
        async with _mock_client(BODY_PAYLOAD) as client:
            adapter = LawGoKrAdapter(oc="secret-oc", client=client)
            body = await adapter.fetch_law(mst="999001")
            assert body.law_name == "건축법"
            assert body.law_type == "법률"
            assert body.ministry == "국토교통부"
            assert body.articles[0]["조문번호"] == "1"
            assert body.addenda[0]["부칙공포일자"] == "20260101"
            assert body.attachments[0]["별표번호"] == "1"

    asyncio.run(run())


def test_effective_body_passes_effective_date():
    async def run():
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.url.params)
            return httpx.Response(200, json=BODY_PAYLOAD, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = LawGoKrAdapter(oc="secret-oc", client=client)
            await adapter.fetch_effective_law(
                mst="999001",
                effective_date=date(2025, 1, 1),
                article="001000",
            )
        assert seen["target"] == "eflaw"
        assert seen["efYd"] == "20250101"
        assert seen["JO"] == "001000"

    asyncio.run(run())
