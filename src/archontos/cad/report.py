"""MVP-5 compliance report: deterministic JSON and self-contained HTML (Korean labels).

Pure Python, no I/O, no clock. The caller maps DB rows to the input dataclasses below:

* :class:`ReportRule` <- one ``cad_check_result`` row (``result_view``): ``outcome``, ``reason``,
  ``rule_version_id``, ``rule_title`` -> ``rule_label``, ``jurisdiction_code``,
  ``effective.valid_from/valid_to``, ``evidence_locator`` (first entry's ``locator`` rendered as
  ``evidence_locator`` text), ``arch_object_ids`` / ``entity_handles`` -> ``objects``.
  ``article_excerpt`` is the legal article text the evidence span points at (may be empty).
* :class:`ReportObject` <- an offending CAD object (``kind``, DXF ``handle``, ``ifc_guid``).
* :class:`ReportTransition` <- one entry of ``cad_revision_diff.transitions_json``
  (``rule_version_id``, ``rule_title``, ``base_outcome``, ``target_outcome``).
* :class:`ReportStale` <- one stale marker (``stale_by_diff_ids`` of a check run, or
  ``cad_revision_diff.stale_json``): ``diff_id`` and an optional ``reason``.
* :class:`ReportDiff` <- ``cad_revision_diff`` counts plus the two lists above.
* :class:`ComplianceReport` is the root; ``generated_at`` is passed in (never ``now()``).

Same input gives identical bytes: sequences keep the caller's order except rules, which are
sorted by ``(rule_version_id)``, and all text is HTML-escaped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from html import escape
from typing import Any

REPORT_VERSION = "1"

DISCLAIMER = (
    "본 보고서는 승인된 규칙에 따른 자동 검토 결과이며, AI가 허가 적합성을 보장하지 않습니다."
)

OUTCOME_LABELS: dict[str, str] = {"PASS": "적합", "FAIL": "부적합", "REVIEW": "검토 필요"}
_OUTCOME_ORDER = ("PASS", "FAIL", "REVIEW")
_KIND_LABELS: dict[str, str] = {
    "wall": "벽",
    "door": "문",
    "window": "창",
    "space": "실",
    "stair": "계단",
    "column": "기둥",
    "opening": "개구부",
}


@dataclass(frozen=True, slots=True)
class ReportObject:
    kind: str
    handle: str | None = None
    ifc_guid: str | None = None


@dataclass(frozen=True, slots=True)
class ReportRule:
    rule_version_id: str
    rule_label: str
    outcome: str  # PASS | FAIL | REVIEW
    jurisdiction_code: str
    valid_from: str | None = None  # ISO date
    valid_to: str | None = None  # ISO date, None = open ended
    reason: str | None = None
    evidence_locator: str | None = None
    article_excerpt: str | None = None
    objects: tuple[ReportObject, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportTransition:
    rule_version_id: str
    rule_label: str
    base_outcome: str | None
    target_outcome: str | None


@dataclass(frozen=True, slots=True)
class ReportStale:
    diff_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReportDiff:
    base_revision_id: str
    target_revision_id: str
    added_count: int = 0
    removed_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    transitions: tuple[ReportTransition, ...] = ()
    stale: tuple[ReportStale, ...] = ()


@dataclass(frozen=True, slots=True)
class ComplianceReport:
    title: str
    revision_id: str
    run_id: str
    jurisdiction_code: str
    reference_date: str  # ISO date
    generated_at: str  # ISO timestamp supplied by the caller
    rules: tuple[ReportRule, ...] = ()
    not_applicable_count: int = 0
    stale_by_diff_ids: tuple[str, ...] = ()
    diff: ReportDiff | None = None


def _counts(report: ComplianceReport) -> dict[str, int]:
    out = {"total": len(report.rules), "PASS": 0, "FAIL": 0, "REVIEW": 0}
    for r in report.rules:
        if r.outcome in out:
            out[r.outcome] += 1
    out["not_applicable"] = report.not_applicable_count
    return out


def _sorted_rules(report: ComplianceReport) -> list[ReportRule]:
    return sorted(report.rules, key=lambda r: r.rule_version_id)


def _obj(o: ReportObject) -> dict[str, Any]:
    return {"kind": o.kind, "handle": o.handle, "ifc_guid": o.ifc_guid}


def _transition_kind(t: ReportTransition) -> str:
    if t.base_outcome == "PASS" and t.target_outcome == "FAIL":
        return "PASS->FAIL"
    return f"{t.base_outcome or 'N/A'}->{t.target_outcome or 'N/A'}"


def render_json(report: ComplianceReport) -> dict[str, Any]:
    """Plain JSON-able dict with a stable key order (insertion order is the contract)."""
    diff: dict[str, Any] | None = None
    if report.diff is not None:
        d = report.diff
        diff = {
            "base_revision_id": d.base_revision_id,
            "target_revision_id": d.target_revision_id,
            "counts": {
                "added": d.added_count,
                "removed": d.removed_count,
                "modified": d.modified_count,
                "unchanged": d.unchanged_count,
            },
            "transitions": [
                {
                    "rule_version_id": t.rule_version_id,
                    "rule_label": t.rule_label,
                    "base_outcome": t.base_outcome,
                    "target_outcome": t.target_outcome,
                    "transition": _transition_kind(t),
                    "pass_to_fail": _transition_kind(t) == "PASS->FAIL",
                }
                for t in d.transitions
            ],
            "stale": [{"diff_id": s.diff_id, "reason": s.reason} for s in d.stale],
        }
    return {
        "report_version": REPORT_VERSION,
        "title": report.title,
        "revision_id": report.revision_id,
        "run_id": report.run_id,
        "jurisdiction_code": report.jurisdiction_code,
        "reference_date": report.reference_date,
        "generated_at": report.generated_at,
        "disclaimer": DISCLAIMER,
        "summary": _counts(report),
        "stale": bool(report.stale_by_diff_ids),
        "stale_by_diff_ids": list(report.stale_by_diff_ids),
        "rules": [
            {
                "rule_version_id": r.rule_version_id,
                "rule_label": r.rule_label,
                "outcome": r.outcome,
                "jurisdiction_code": r.jurisdiction_code,
                "effective": {"valid_from": r.valid_from, "valid_to": r.valid_to},
                "reason": r.reason,
                "evidence_locator": r.evidence_locator,
                "article_excerpt": r.article_excerpt,
                "objects": [_obj(o) for o in r.objects],
            }
            for r in _sorted_rules(report)
        ],
        "diff": diff,
    }


def canonical_bytes(report: ComplianceReport) -> bytes:
    """Canonical UTF-8 JSON of :func:`render_json` (sorted keys, no whitespace)."""
    return json.dumps(
        render_json(report), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_sha256(report: ComplianceReport) -> str:
    """sha256 of the canonical JSON; stable across runs for the same input."""
    return hashlib.sha256(canonical_bytes(report)).hexdigest()


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


_CSS = (
    "body{font-family:'Malgun Gothic','Noto Sans KR',sans-serif;margin:2rem;color:#1a1a1a}"
    "h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:1.8rem}"
    "table{border-collapse:collapse;width:100%;font-size:.9rem}"
    "th,td{border:1px solid #bbb;padding:.35rem .5rem;text-align:left;vertical-align:top}"
    "th{background:#f0f0f0}.PASS{color:#146c2e}.FAIL{color:#b3261e;font-weight:bold}"
    ".REVIEW{color:#8a5a00}.stale{background:#fff4d6;border:1px solid #d9a400;padding:.5rem}"
    ".disclaimer{margin-top:2rem;padding:.6rem;border:1px solid #888;background:#f7f7f7}"
    ".muted{color:#666}"
)


def _outcome_cell(outcome: str | None) -> str:
    if outcome is None:
        return '<span class="muted">없음</span>'
    label = OUTCOME_LABELS.get(outcome, outcome)
    return f'<span class="{_e(outcome)}">{_e(label)}</span>'


def _objects_html(objs: tuple[ReportObject, ...]) -> str:
    if not objs:
        return '<span class="muted">-</span>'
    items = []
    for o in objs:
        parts = [_e(_KIND_LABELS.get(o.kind, o.kind))]
        if o.handle:
            parts.append(f"핸들 {_e(o.handle)}")
        if o.ifc_guid:
            parts.append(f"IFC {_e(o.ifc_guid)}")
        items.append("<li>" + " / ".join(parts) + "</li>")
    return "<ul>" + "".join(items) + "</ul>"


def render_html(report: ComplianceReport) -> str:
    """Self-contained HTML document: inline CSS, no scripts, no external assets."""
    c = _counts(report)
    rules = _sorted_rules(report)
    out: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="ko"><head><meta charset="utf-8">',
        f"<title>{_e(report.title)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_e(report.title)}</h1>",
        "<table><tbody>",
        f"<tr><th>리비전</th><td>{_e(report.revision_id)}</td></tr>",
        f"<tr><th>검토 실행</th><td>{_e(report.run_id)}</td></tr>",
        f"<tr><th>관할</th><td>{_e(report.jurisdiction_code)}</td></tr>",
        f"<tr><th>기준일</th><td>{_e(report.reference_date)}</td></tr>",
        f"<tr><th>생성 시각</th><td>{_e(report.generated_at)}</td></tr>",
        "</tbody></table>",
    ]
    if report.stale_by_diff_ids:
        ids = ", ".join(_e(i) for i in report.stale_by_diff_ids)
        out.append(
            '<p class="stale">이 검토 결과는 이후 리비전 변경으로 인해 최신이 아닙니다 '
            f"(변경 비교: {ids}).</p>"
        )
    out += [
        "<h2>요약</h2>",
        "<table><thead><tr><th>전체 규칙</th><th>적합</th><th>부적합</th>"
        "<th>검토 필요</th><th>해당 없음</th></tr></thead><tbody>",
        f'<tr><td>{c["total"]}</td><td class="PASS">{c["PASS"]}</td>'
        f'<td class="FAIL">{c["FAIL"]}</td><td class="REVIEW">{c["REVIEW"]}</td>'
        f"<td>{c['not_applicable']}</td></tr></tbody></table>",
        "<h2>규칙별 결과</h2>",
    ]
    if not rules:
        out.append('<p class="muted">적용된 규칙이 없습니다.</p>')
    else:
        out.append(
            "<table><thead><tr><th>규칙</th><th>결과</th><th>관할</th><th>시행 기간</th>"
            "<th>근거 위치</th><th>조문 발췌</th><th>사유</th><th>문제 객체</th></tr>"
            "</thead><tbody>"
        )
        for r in rules:
            period = f"{_e(r.valid_from or '')} ~ {_e(r.valid_to or '')}"
            out.append(
                f'<tr><td>{_e(r.rule_label)}<br><span class="muted">{_e(r.rule_version_id)}'
                f"</span></td><td>{_outcome_cell(r.outcome)}</td><td>{_e(r.jurisdiction_code)}</td>"
                f"<td>{period}</td><td>{_e(r.evidence_locator)}</td>"
                f"<td>{_e(r.article_excerpt)}</td><td>{_e(r.reason)}</td>"
                f"<td>{_objects_html(r.objects)}</td></tr>"
            )
        out.append("</tbody></table>")

    if report.diff is not None:
        d = report.diff
        out += [
            "<h2>리비전 변경</h2>",
            f"<p>기준 {_e(d.base_revision_id)} → 대상 {_e(d.target_revision_id)}</p>",
            "<table><thead><tr><th>추가</th><th>삭제</th><th>수정</th><th>변경 없음</th>"
            "</tr></thead><tbody>",
            f"<tr><td>{d.added_count}</td><td>{d.removed_count}</td><td>{d.modified_count}</td>"
            f"<td>{d.unchanged_count}</td></tr></tbody></table>",
            "<h3>규칙 결과 전이</h3>",
        ]
        if not d.transitions:
            out.append('<p class="muted">전이 내역이 없습니다.</p>')
        else:
            out.append(
                "<table><thead><tr><th>규칙</th><th>이전</th><th>이후</th><th>비고</th>"
                "</tr></thead><tbody>"
            )
            for t in d.transitions:
                flag = "적합 → 부적합" if _transition_kind(t) == "PASS->FAIL" else ""
                out.append(
                    f"<tr><td>{_e(t.rule_label)}</td><td>{_outcome_cell(t.base_outcome)}</td>"
                    f"<td>{_outcome_cell(t.target_outcome)}</td><td>{_e(flag)}</td></tr>"
                )
            out.append("</tbody></table>")
        out.append("<h3>최신 아님 표시</h3>")
        if not d.stale:
            out.append('<p class="muted">없음</p>')
        else:
            out.append("<ul>")
            for s in d.stale:
                out.append(f"<li>{_e(s.diff_id)} {_e(s.reason)}</li>")
            out.append("</ul>")

    out += [f'<p class="disclaimer">{_e(DISCLAIMER)}</p>', "</body></html>"]
    return "\n".join(out) + "\n"
