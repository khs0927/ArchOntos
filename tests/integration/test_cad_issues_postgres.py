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
