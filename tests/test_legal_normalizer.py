from dataclasses import dataclass
from datetime import date

from archontos.normalization.legal import LawEvidenceNormalizer


@dataclass
class Body:
    law_id: str = "001823"
    mst: str = "999001"
    enforcement_date: date = date(2026, 7, 1)
    articles: tuple = (
        {
            "조문번호": "10",
            "조문가지번호": "2",
            "조문제목": "피난시설",
            "조문내용": "제10조의2 전체 조문",
            "항": {
                "항번호": "1",
                "항내용": "① 직통계단을 설치하여야 한다.",
                "호": {
                    "호번호": "2",
                    "호내용": "2. 두 개소 이상 설치",
                    "목": [
                        {"목번호": "가", "목내용": "가. 첫 번째 조건"},
                        {"목번호": "나", "목내용": "나. 두 번째 조건"},
                    ],
                },
            },
        },
    )
    addenda: tuple = (
        {"부칙공포일자": "20260101", "부칙공포번호": "21000", "부칙내용": "부칙 내용"},
    )
    attachments: tuple = (
        {
            "별표번호": "1",
            "별표가지번호": "0",
            "별표제목": "용도별 건축물의 종류",
            "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=123",
        },
    )


def test_structured_body_becomes_hierarchical_evidence_units():
    units = LawEvidenceNormalizer().normalize(Body())
    assert [unit.kind for unit in units] == [
        "article",
        "paragraph",
        "subparagraph",
        "item",
        "item",
        "addendum",
        "attachment",
    ]

    item = units[3]
    assert item.locator["article_no"] == "10"
    assert item.locator["article_branch_no"] == "2"
    assert item.locator["paragraph_no"] == "1"
    assert item.locator["subparagraph_no"] == "2"
    assert item.locator["item_no"] == "가"
    assert item.locator["effective_date"] == "2026-07-01"
    assert item.extractor_method == "structured-parser"
    assert item.extraction_confidence == 1.0


def test_normalization_is_deterministic_and_keys_are_unique():
    normalizer = LawEvidenceNormalizer()
    first = normalizer.normalize(Body())
    second = normalizer.normalize(Body())

    assert first == second
    assert len({unit.evidence_key for unit in first}) == len(first)
    assert all(unit.normalized_text_hash for unit in first)


def test_attachment_relative_link_is_preserved_as_official_absolute_url():
    units = LawEvidenceNormalizer().normalize(Body())
    attachment = units[-1]
    assert attachment.locator["pdf_url"] == ("https://www.law.go.kr/LSW/flDownload.do?flSeq=123")
    assert attachment.text_snippet == "용도별 건축물의 종류"


def test_missing_official_numbers_still_produce_unique_fallback_identity():
    body = Body()
    body.articles = (
        {"조문내용": "첫 번째 번호 누락 조문"},
        {"조문내용": "두 번째 번호 누락 조문"},
    )
    units = LawEvidenceNormalizer().normalize(body)
    assert len(units) == 4
    assert units[0].evidence_key != units[1].evidence_key
    assert units[0].locator["article_index"] == 1
    assert units[1].locator["article_index"] == 2


def test_numbered_evidence_identity_survives_reordering():
    normalizer = LawEvidenceNormalizer()
    original = normalizer.normalize(Body())
    original_article = next(
        unit for unit in original if unit.kind == "article" and unit.locator["article_no"] == "10"
    )

    reordered = Body()
    reordered.articles = (
        {
            "조문번호": "9",
            "조문제목": "새 조문",
            "조문내용": "앞에 삽입된 조문",
        },
        *Body().articles,
    )
    shifted = normalizer.normalize(reordered)
    shifted_article = next(
        unit for unit in shifted if unit.kind == "article" and unit.locator["article_no"] == "10"
    )

    assert original_article.locator["article_index"] == 1
    assert shifted_article.locator["article_index"] == 2
    assert original_article.evidence_key == shifted_article.evidence_key
