from __future__ import annotations

import json

from archontos.cad.report import (
    DISCLAIMER,
    ComplianceReport,
    ReportDiff,
    ReportObject,
    ReportRule,
    ReportStale,
    ReportTransition,
    content_sha256,
    render_html,
    render_json,
)


def _report(**kw: object) -> ComplianceReport:
    rules = (
        ReportRule(
            rule_version_id="rv-2",
            rule_label="문 폭 0.9m 이상",
            outcome="FAIL",
            jurisdiction_code="KR-11",
            valid_from="2024-01-01",
            evidence_locator="제10조 제1항",
            article_excerpt="출입문의 폭은 <b>0.9</b>m 이상 & 안전",
            reason="door.width < 0.9",
            objects=(ReportObject("door", "2A", "3cUkl32yn9qRSPvBJVyWYp"),),
        ),
        ReportRule("rv-1", "계단 폭", "PASS", "KR", "2023-01-01", "2030-01-01"),
        ReportRule("rv-3", "복도 폭", "REVIEW", "KR-11", reason="missing fact"),
    )
    base: dict[str, object] = {
        "title": "검토 보고서",
        "revision_id": "rev-1",
        "run_id": "run-1",
        "jurisdiction_code": "KR-11",
        "reference_date": "2026-01-01",
        "generated_at": "2026-02-03T04:05:06Z",
        "rules": rules,
        "not_applicable_count": 4,
    }
    base.update(kw)
    return ComplianceReport(**base)  # type: ignore[arg-type]


def test_determinism() -> None:
    a, b = _report(), _report()
    assert render_html(a).encode() == render_html(b).encode()
    assert json.dumps(render_json(a)) == json.dumps(render_json(b))
    assert content_sha256(a) == content_sha256(b)
    assert len(content_sha256(a)) == 64


def test_hash_changes_with_input() -> None:
    assert content_sha256(_report()) != content_sha256(_report(title="다른"))


def test_rule_order_independent_of_input_order() -> None:
    r = _report()
    rev = _report(rules=tuple(reversed(r.rules)))
    assert render_html(r) == render_html(rev)
    assert [x["rule_version_id"] for x in render_json(r)["rules"]] == ["rv-1", "rv-2", "rv-3"]


def test_counts() -> None:
    s = render_json(_report())["summary"]
    assert s == {"total": 3, "PASS": 1, "FAIL": 1, "REVIEW": 1, "not_applicable": 4}


def test_escaping() -> None:
    html = render_html(_report(title="<script>alert(1)</script>"))
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;0.9&lt;/b&gt;m 이상 &amp; 안전" in html
    assert "<b>0.9" not in html


def test_self_contained() -> None:
    html = render_html(_report())
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and "<link" not in html


def test_disclaimer_present() -> None:
    assert DISCLAIMER in render_html(_report())
    assert render_json(_report())["disclaimer"] == DISCLAIMER
    assert DISCLAIMER in render_html(_report(rules=()))


def test_objects_rendered() -> None:
    html = render_html(_report())
    assert "핸들 2A" in html and "3cUkl32yn9qRSPvBJVyWYp" in html
    assert render_json(_report())["rules"][1]["objects"] == [
        {"kind": "door", "handle": "2A", "ifc_guid": "3cUkl32yn9qRSPvBJVyWYp"}
    ]


def test_transitions_and_stale() -> None:
    diff = ReportDiff(
        base_revision_id="rev-1",
        target_revision_id="rev-2",
        added_count=1,
        modified_count=2,
        transitions=(
            ReportTransition("rv-2", "문 폭", "PASS", "FAIL"),
            ReportTransition("rv-9", "신규", None, "REVIEW"),
        ),
        stale=(ReportStale("diff-1", "후속 리비전"),),
    )
    rep = _report(diff=diff, stale_by_diff_ids=("diff-1",))
    html = render_html(rep)
    assert "적합 → 부적합" in html
    assert "최신이 아닙니다" in html and "diff-1" in html
    j = render_json(rep)
    assert j["stale"] is True
    assert j["diff"]["transitions"][0]["pass_to_fail"] is True
    assert j["diff"]["transitions"][1]["transition"] == "N/A->REVIEW"
    assert j["diff"]["counts"]["modified"] == 2


def test_empty_report() -> None:
    rep = _report(rules=(), not_applicable_count=0)
    html = render_html(rep)
    assert "적용된 규칙이 없습니다" in html
    j = render_json(rep)
    assert j["rules"] == [] and j["diff"] is None and j["stale"] is False
    assert j["summary"]["total"] == 0
    assert "최신이 아닙니다" not in html
