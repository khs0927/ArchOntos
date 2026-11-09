"""MVP-3 revision diff through the real ingestion app: upload two revisions, check, diff (RLS)."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import apps.ingestion as ingestion_app
from archontos.storage.artifacts import LocalArtifactStore
from tests.cad_fixtures import revision_plan
from tests.integration.conftest import count_rows
from tests.integration.test_cad_checks_postgres import HEADERS, _active_rules

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(mvp0_db, monkeypatch, tmp_path):
    session_factory, _schema = mvp0_db
    monkeypatch.setattr(ingestion_app, "get_session_factory", lambda: session_factory)
    store = LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(ingestion_app, "_artifact_store", lambda: store)
    transport = httpx.ASGITransport(app=ingestion_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http, session_factory, store


async def _upload(http, data: bytes, source_key: str) -> str:
    res = await http.post(
        "/v1/cad/artifacts",
        params={"project_id": "P-3", "source_key": source_key},
        content=data,
        headers=HEADERS,
    )
    assert res.status_code == 201, res.text
    return res.json()["revision"]["id"]


async def test_revision_diff_impact_transitions_and_stale(client, monkeypatch):
    http, factory, store = client
    permit = await _upload(http, revision_plan(), "A-301")
    built = await _upload(
        http, revision_plan(door_x=1500, corridor_width=900, extra_space=True), "A-301"
    )
    other = await _upload(http, revision_plan(corridor_width=1300), "A-302")
    (rule_id,) = await _active_rules(
        factory,
        store,
        [
            {
                "title": "복도 폭 1.0m",
                "fact_path": "corridor.width",
                "operator": ">=",
                "value": 1.0,
                "unit": "m",
            }
        ],
    )
    checks = await http.post(
        f"/v1/cad/revisions/{permit}/checks",
        json={"jurisdiction_code": "KR", "reference_date": "2026-11-01"},
    )
    assert checks.status_code == 201, checks.text
    assert [r["outcome"] for r in checks.json()["results"]] == ["PASS"]
    assert (await http.get(f"/v1/cad/revisions/{permit}/checks")).json()["items"][0][
        "stale"
    ] is False

    body = {"base_revision_id": permit, "target_revision_id": built, "rerun_checks": True}
    first = await http.post("/v1/cad/diffs", json=body)
    assert first.status_code == 201, first.text
    diff = first.json()["diff"]
    assert first.json()["created"] and diff["relation"] == "lineage"
    assert diff["added_count"] == 1 and diff["modified_count"] >= 2
    assert diff["jurisdiction_code"] == "KR"
    (transition,) = [t for t in diff["transitions"] if t["rule_version_id"] == rule_id]
    assert transition["transition"] == "PASS->FAIL" and transition["regression"]
    assert diff["summary"]["transition_counts"]["regressions"] == 1
    assert diff["stale"]["carried_over"] is False and diff["stale"]["stale_result_ids"]

    # the base revision's checks are now stale, the target has its own fresh verdicts
    runs = (await http.get(f"/v1/cad/revisions/{permit}/checks")).json()["items"]
    assert runs[0]["stale"] is True and runs[0]["stale_by_diff_ids"] == [diff["id"]]
    target_runs = (await http.get(f"/v1/cad/revisions/{built}/checks")).json()["items"]
    assert len(target_runs) == 1 and target_runs[0]["fail_count"] == 1
    assert target_runs[0]["stale"] is False

    again = await http.post("/v1/cad/diffs", json=body)
    assert again.status_code == 200 and again.json()["diff"]["id"] == diff["id"]
    assert again.json()["created"] is False
    assert await count_rows(factory, "cad_revision_diff") == 1
    assert await count_rows(factory, "outbox_message", "topic = 'cad.revision-diffed'") == 1
    assert await count_rows(factory, "audit_log", "action = 'cad.revision.diff'") == 1
    assert (await http.get(f"/v1/cad/diffs/{diff['id']}")).json()["id"] == diff["id"]

    changes_url = f"/v1/cad/diffs/{diff['id']}/changes"
    added = (await http.get(changes_url, params={"change_type": "added"})).json()["items"]
    assert [c["kind"] for c in added] == ["space"] and added[0]["label"] == "창고"
    spaces = (
        await http.get(changes_url, params={"kind": "space", "change_type": "modified"})
    ).json()
    corridor = next(c for c in spaces["items"] if c["label"] == "복도")
    assert any(d["property"] == "area" and d["unit"] == "m2" for d in corridor["deltas"])
    assert any(r["rule_version_id"] == rule_id for r in corridor["impact"]["base"])
    assert all(r["stale"] for r in corridor["impact"]["base"])
    seen, cursor = [], None
    for _ in range(40):
        page = (
            await http.get(
                changes_url, params={"limit": 2, **({"cursor": cursor} if cursor else {})}
            )
        ).json()
        seen += [c["id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    total = diff["added_count"] + diff["removed_count"] + diff["modified_count"]
    assert len(seen) == len(set(seen)) == total + diff["unchanged_count"] and cursor is None
    bad = await http.get(changes_url, params={"kind": "wall", "cursor": "x"})
    assert bad.status_code == 400

    # a different rerun flag or tolerance set is a different diff
    plain = await http.post("/v1/cad/diffs", json={**body, "rerun_checks": False})
    assert plain.status_code == 201 and plain.json()["diff"]["id"] != diff["id"]
    assert plain.json()["diff"]["transitions"] == []
    loose = await http.post("/v1/cad/diffs", json={**body, "tolerances": {"match_centroid": 0.25}})
    assert loose.status_code == 201 and loose.json()["diff"]["id"] != diff["id"]

    # explicit pairing of unrelated drawings
    paired = await http.post(
        "/v1/cad/diffs", json={"base_revision_id": permit, "target_revision_id": other}
    )
    assert paired.status_code == 201 and paired.json()["diff"]["relation"] == "paired"

    same = await http.post(
        "/v1/cad/diffs", json={"base_revision_id": permit, "target_revision_id": permit}
    )
    assert same.status_code == 422
    missing = await http.post(
        "/v1/cad/diffs",
        json={
            "base_revision_id": permit,
            "target_revision_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert missing.status_code == 404
    bad_tol = await http.post("/v1/cad/diffs", json={**body, "tolerances": {"nope": 1}})
    assert bad_tol.status_code == 422

    for statement in (
        "UPDATE cad_object_change SET label = 'x'",
        "DELETE FROM cad_revision_diff",
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement))

    from archontos.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_jurisdictions", "JP")
    assert (await http.get(f"/v1/cad/diffs/{diff['id']}")).status_code == 404
    assert (await http.get(changes_url)).status_code == 404
    assert (await http.post("/v1/cad/diffs", json=body)).status_code == 404
