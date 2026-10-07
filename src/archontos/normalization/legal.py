from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict) and "content" in value:
        value = value["content"]
    text = str(value).strip()
    return text or None


def _children(node: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = node.get(key)
        if value is not None:
            return [item for item in _as_list(value) if isinstance(item, dict)]
    return []


def _official_url(value: Any) -> str | None:
    text = _string(value)
    if not text:
        return None
    if text.startswith("/"):
        return f"https://www.law.go.kr{text}"
    return text


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _text_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(_normalized_text(value).encode("utf-8")).hexdigest()


def _locator_key(locator: dict[str, Any]) -> str:
    identity: dict[str, Any] = {"kind": locator["kind"]}

    article_no = locator.get("article_no")
    if article_no not in (None, ""):
        identity["article_no"] = article_no
        branch_no = locator.get("article_branch_no")
        if branch_no not in (None, ""):
            identity["article_branch_no"] = branch_no
    elif locator.get("article_index") not in (None, ""):
        identity["article_index"] = locator["article_index"]

    paragraph_no = locator.get("paragraph_no")
    if paragraph_no not in (None, ""):
        identity["paragraph_no"] = paragraph_no
    elif locator.get("paragraph_index") not in (None, ""):
        identity["paragraph_index"] = locator["paragraph_index"]

    subparagraph_no = locator.get("subparagraph_no")
    if subparagraph_no not in (None, ""):
        identity["subparagraph_no"] = subparagraph_no
    elif locator.get("subparagraph_index") not in (None, ""):
        identity["subparagraph_index"] = locator["subparagraph_index"]

    item_no = locator.get("item_no")
    if item_no not in (None, ""):
        identity["item_no"] = item_no
    elif locator.get("item_index") not in (None, ""):
        identity["item_index"] = locator["item_index"]

    if locator["kind"] == "addendum":
        promulgation_date = locator.get("addendum_promulgation_date")
        promulgation_no = locator.get("addendum_promulgation_no")
        if promulgation_date not in (None, ""):
            identity["addendum_promulgation_date"] = promulgation_date
        if promulgation_no not in (None, ""):
            identity["addendum_promulgation_no"] = promulgation_no
        if (
            promulgation_date in (None, "")
            and promulgation_no in (None, "")
            and locator.get("addendum_index") not in (None, "")
        ):
            identity["addendum_index"] = locator["addendum_index"]

    if locator["kind"] == "attachment":
        attachment_no = locator.get("attachment_no")
        if attachment_no not in (None, ""):
            identity["attachment_no"] = attachment_no
            branch_no = locator.get("attachment_branch_no")
            if branch_no not in (None, ""):
                identity["attachment_branch_no"] = branch_no
        elif locator.get("attachment_index") not in (None, ""):
            identity["attachment_index"] = locator["attachment_index"]

    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    kind = str(locator["kind"])
    return f"lawgo:{kind}:{digest}"


@dataclass(frozen=True, slots=True)
class LegalEvidenceUnit:
    evidence_key: str
    kind: str
    locator: dict[str, Any]
    text_snippet: str | None
    normalized_text_hash: str | None
    extractor_method: str = "structured-parser"
    extraction_confidence: float = 1.0


class LawEvidenceNormalizer:
    """Deterministically map structured law.go.kr body fields to evidence units."""

    source_name = "law.go.kr"

    def normalize(self, body: Any) -> tuple[LegalEvidenceUnit, ...]:
        units: list[LegalEvidenceUnit] = []
        effective_date = self._date_text(getattr(body, "enforcement_date", None))
        base = {
            "source": self.source_name,
            "law_id": getattr(body, "law_id", "") or None,
            "mst": getattr(body, "mst", None),
            "effective_date": effective_date,
        }

        for article_index, article in enumerate(getattr(body, "articles", ()), start=1):
            if not isinstance(article, dict):
                continue
            units.extend(self._normalize_article(base, article, article_index))

        for index, addendum in enumerate(getattr(body, "addenda", ()), start=1):
            if not isinstance(addendum, dict):
                continue
            locator = {
                **base,
                "kind": "addendum",
                "addendum_index": index,
                "addendum_promulgation_date": _string(addendum.get("부칙공포일자")),
                "addendum_promulgation_no": _string(addendum.get("부칙공포번호")),
            }
            units.append(self._unit(locator, _string(addendum.get("부칙내용"))))

        for index, attachment in enumerate(getattr(body, "attachments", ()), start=1):
            if not isinstance(attachment, dict):
                continue
            locator = {
                **base,
                "kind": "attachment",
                "attachment_index": index,
                "attachment_no": _string(attachment.get("별표번호")),
                "attachment_branch_no": _string(attachment.get("별표가지번호")),
                "attachment_type": _string(attachment.get("별표구분")),
                "attachment_title": _string(
                    attachment.get("별표제목문자열") or attachment.get("별표제목")
                ),
                "hwp_url": _official_url(attachment.get("별표서식파일링크")),
                "pdf_url": _official_url(attachment.get("별표서식PDF파일링크")),
                "image_url": _official_url(attachment.get("별표서식이미지파일링크")),
            }
            text = _string(attachment.get("별표내용")) or _string(locator["attachment_title"])
            units.append(self._unit(locator, text))

        return tuple(units)

    def _normalize_article(
        self, base: dict[str, Any], article: dict[str, Any], article_index: int
    ) -> list[LegalEvidenceUnit]:
        article_no = _string(article.get("조문번호"))
        article_branch_no = _string(article.get("조문가지번호"))
        common = {
            **base,
            "article_index": article_index,
            "article_no": article_no,
            "article_branch_no": article_branch_no,
            "article_title": _string(article.get("조문제목")),
            "article_effective_date": _string(article.get("조문시행일자")),
        }
        units = [self._unit({**common, "kind": "article"}, _string(article.get("조문내용")))]

        paragraphs = _children(article, "항", "항단위")
        for paragraph_index, paragraph in enumerate(paragraphs, start=1):
            units.extend(self._normalize_paragraph(common, paragraph, paragraph_index))

        if not paragraphs:
            for subparagraph_index, subparagraph in enumerate(
                _children(article, "호", "호단위"), start=1
            ):
                units.extend(self._normalize_subparagraph(common, subparagraph, subparagraph_index))

        return units

    def _normalize_paragraph(
        self, common: dict[str, Any], paragraph: dict[str, Any], paragraph_index: int
    ) -> list[LegalEvidenceUnit]:
        paragraph_common = {
            **common,
            "paragraph_index": paragraph_index,
            "paragraph_no": _string(paragraph.get("항번호")),
        }
        units = [
            self._unit({**paragraph_common, "kind": "paragraph"}, _string(paragraph.get("항내용")))
        ]
        for subparagraph_index, subparagraph in enumerate(
            _children(paragraph, "호", "호단위"), start=1
        ):
            units.extend(
                self._normalize_subparagraph(paragraph_common, subparagraph, subparagraph_index)
            )
        return units

    def _normalize_subparagraph(
        self, common: dict[str, Any], subparagraph: dict[str, Any], subparagraph_index: int
    ) -> list[LegalEvidenceUnit]:
        subparagraph_common = {
            **common,
            "subparagraph_index": subparagraph_index,
            "subparagraph_no": _string(subparagraph.get("호번호")),
        }
        units = [
            self._unit(
                {**subparagraph_common, "kind": "subparagraph"},
                _string(subparagraph.get("호내용")),
            )
        ]
        for item_index, item in enumerate(_children(subparagraph, "목", "목단위"), start=1):
            locator = {
                **subparagraph_common,
                "kind": "item",
                "item_index": item_index,
                "item_no": _string(item.get("목번호")),
            }
            units.append(self._unit(locator, _string(item.get("목내용"))))
        return units

    @staticmethod
    def _date_text(value: Any) -> str | None:
        if isinstance(value, date):
            return value.isoformat()
        return _string(value)

    @staticmethod
    def _unit(locator: dict[str, Any], text: str | None) -> LegalEvidenceUnit:
        clean_locator = {key: value for key, value in locator.items() if value is not None}
        return LegalEvidenceUnit(
            evidence_key=_locator_key(clean_locator),
            kind=str(clean_locator["kind"]),
            locator=clean_locator,
            text_snippet=text,
            normalized_text_hash=_text_hash(text),
        )
