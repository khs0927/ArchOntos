"""MVP-5 issues + BCF export through the real ingestion app (append-only, RLS, outbox)."""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.cad_fixtures import revision_plan
from tests.integration.conftest import count_rows
from tests.integration.test_cad_checks_postgres import _active_rules
from tests.integration.test_cad_diff_postgres import _upload, client  # noqa: F401

pytestmark = pytest.mark.asyncio

RULE = {
    "title": "복도 폭 1.0m",
    "fact_path": "corridor.width",
    "operator": ">=",
    "value": 1.0,
    "unit": "m",
}


async def _check(http, revision_id: str) -> str:
    res = await http.post(
        f"/v1/cad/revisions/{revision_id}/checks",
        json={"jurisdiction_code": "KR", "reference_date": "2026-11-01"},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["run"]["id"]


async def test_issues_derive_status_and_bcf(client, monkeypatch):  # noqa: F811
    http, factory, store = client
    permit = await _upload(http, revision_plan(), "A-501")
    built = await _upload(http, revision_plan(corridor_width=900), "A-501")
    (rule_id,) = await _active_rules(factory, store, [RULE])
    permit_run = await _check(http, permit)
    diff = await http.post(
        "/v1/cad/diffs",
        json={"base_revision_id": permit, "target_revision_id": built, "rerun_checks": True},
    )
    assert diff.status_code == 201, diff.text
    diff_id = diff.json()["diff"]["id"]
    built_run = (await http.get(f"/v1/cad/revisions/{built}/checks")).json()["items"][0]["id"]

    # PASS run -> no issues
    none = await http.post(f"/v1/cad/checks/{permit_run}/issues")
    assert none.status_code == 200 and none.json()["items"] == []

    first = await http.post(f"/v1/cad/checks/{built_run}/issues")
    assert first.status_code == 200, first.text
    items = first.json()["items"]
    assert items and first.json()["new_issue_count"] == len(items)
    assert all(i["rule_version_id"] == rule_id and i["outcome"] == "FAIL" for i in items)
    assert all(i["origin"] == "regression" and i["diff_id"] == diff_id for i in items)
    assert all(i["status"] == "open" and i["jurisdiction_code"] == "KR" for i in items)
    assert await count_rows(factory, "outbox_message", "topic = 'cad.issues-derived'") == 1

    again = await http.post(f"/v1/cad/checks/{built_run}/issues")
    assert [i["id"] for i in again.json()["items"]] == [i["id"] for i in items]
    assert again.json()["new_issue_count"] == 0 and again.json()["new_status_count"] == 0
    assert await count_rows(factory, "outbox_message", "topic = 'cad.issues-derived'") == 1
    assert await count_rows(factory, "audit_log", "action = 'cad.issues.derive'") == 1

    # a newer revision fixes the corridor: the issues resolve (new status row, no rewrite)
    fixed = await _upload(http, revision_plan(corridor_width=1300), "A-501")
    res = await http.post(
        "/v1/cad/diffs",
        json={"base_revision_id": built, "target_revision_id": fixed, "rerun_checks": True},
    )
    assert res.status_code == 201, res.text
    later = await http.post(f"/v1/cad/checks/{built_run}/issues")
    assert later.json()["new_issue_count"] == 0
    assert later.json()["new_status_count"] == len(items)
    assert {i["status"] for i in later.json()["items"]} == {"resolved"}
    assert await count_rows(factory, "outbox_message", "topic = 'cad.issues-derived'") == 2

    listed = await http.get("/v1/cad/issues", params={"run_id": built_run, "status": "resolved"})
    assert len(listed.json()["items"]) == len(items)
    assert listed.json()["items"][0]["status_basis_diff_id"] == res.json()["diff"]["id"]
    open_ = await http.get("/v1/cad/issues", params={"run_id": built_run, "status": "open"})
    assert open_.json()["items"] == []
    seen, cursor = [], None
    for _ in range(20):
        page = (
            await http.get(
                "/v1/cad/issues",
                params={"revision_id": built, "limit": 1, **({"cursor": cursor} if cursor else {})},
            )
        ).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(i["id"] for i in items)
    assert (await http.get("/v1/cad/issues", params={"cursor": "x"})).status_code == 400

    bcf = await http.get("/v1/cad/issues/export.bcf", params={"run_id": built_run})
    assert bcf.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(bcf.content))
    names = zf.namelist()
    assert "bcf.version" in names
    assert sum(n.endswith("/markup.bcf") for n in names) == len(items)
    assert b"Resolved" in zf.read(next(n for n in names if n.endswith("/markup.bcf")))
    assert (await http.get("/v1/cad/issues/export.bcf")).status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await http.post(f"/v1/cad/checks/{missing}/issues")).status_code == 404

    for statement in (
        "UPDATE cad_issue SET label = 'x'",
        "DELETE FROM cad_issue_status",
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement))

    from archontos.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_jurisdictions", "JP")
    assert (await http.get("/v1/cad/issues", params={"run_id": built_run})).json()["items"] == []
    hidden = await http.get("/v1/cad/issues/export.bcf", params={"run_id": built_run})
    assert hidden.status_code == 404


async def test_issue_notes_append_only_and_paged(client):  # noqa: F811
    http, factory, store = client
    built = await _upload(http, revision_plan(corridor_width=900), "A-502")
    await _active_rules(factory, store, [RULE])
    run = await _check(http, built)
    issue = (await http.post(f"/v1/cad/checks/{run}/issues")).json()["items"][0]["id"]
    for n in range(3):
        res = await http.post(f"/v1/cad/issues/{issue}/notes", json={"body": f"메모 {n}"})
        assert res.status_code == 201, res.text
        assert res.json()["kind"] == "note" and res.json()["created_by"]
    seen, cursor = [], None
    for _ in range(10):
        page = (
            await http.get(
                f"/v1/cad/issues/{issue}/notes",
                params={"limit": 2, **({"cursor": cursor} if cursor else {})},
            )
        ).json()
        seen += [n["body"] for n in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ["메모 0", "메모 1", "메모 2"]
    assert (
        await http.get(f"/v1/cad/issues/{issue}/notes", params={"cursor": "x"})
    ).status_code == 400
    blank = await http.post(f"/v1/cad/issues/{issue}/notes", json={"body": "   "})
    assert blank.status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        await http.post(f"/v1/cad/issues/{missing}/notes", json={"body": "x"})
    ).status_code == 404
    assert await count_rows(factory, "audit_log", "action = 'cad.issue.note'") == 3
    async with factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("UPDATE cad_issue_note SET body = 'x'"))


def _bcf_zip(topic: str, comments: list[tuple[str, str]]) -> bytes:
    cs = "".join(
        f'<Comment Guid="{g}"><Date>2026-11-03T01:00:00Z</Date><Author>lee</Author>'
        f"<Comment>{t}</Comment></Comment>"
        for g, t in comments
    )
    markup = (
        f'<Markup><Topic Guid="{topic}" TopicStatus="Resolved"><Comments>{cs}</Comments>'
        f"</Topic></Markup>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bcf.version", '<Version VersionId="3.0"/>')
        zf.writestr(f"{topic}/markup.bcf", markup)
    return buf.getvalue()


async def test_bcf_import_notes_idempotent_unknown_reported(client):  # noqa: F811
    from archontos.cad.bcf import topic_guid

    http, factory, store = client
    built = await _upload(http, revision_plan(corridor_width=900), "A-503")
    await _active_rules(factory, store, [RULE])
    run = await _check(http, built)
    issue = (await http.post(f"/v1/cad/checks/{run}/issues")).json()["items"][0]["id"]
    c1, c2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    data = _bcf_zip(topic_guid(issue), [(c1, "확인 완료")])
    first = await http.post("/v1/cad/issues/import.bcf", content=data)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["matched_count"] == 1 and body["notes_created"] == 2  # status + comment
    again = await http.post("/v1/cad/issues/import.bcf", content=data)
    assert again.json()["notes_created"] == 0 and again.json()["notes_skipped"] == 2
    more = await http.post(
        "/v1/cad/issues/import.bcf",
        content=_bcf_zip(topic_guid(issue), [(c1, "확인 완료"), (c2, "추가")]),
    )
    assert more.json()["notes_created"] == 1
    notes = (await http.get(f"/v1/cad/issues/{issue}/notes")).json()["items"]
    assert sorted(n["kind"] for n in notes) == ["bcf_comment", "bcf_comment", "bcf_status"]
    assert {n["external_author"] for n in notes if n["kind"] == "bcf_comment"} == {"lee"}

    unknown = "44444444-4444-4444-8444-444444444444"
    res = await http.post("/v1/cad/issues/import.bcf", content=_bcf_zip(unknown, [(c1, "x")]))
    assert res.json()["unknown_topic_guids"] == [unknown] and res.json()["notes_created"] == 0
    assert await count_rows(factory, "cad_issue") == 1  # never creates issues
    assert (await http.post("/v1/cad/issues/import.bcf", content=b"junk")).status_code == 422
