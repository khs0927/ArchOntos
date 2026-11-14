"""MVP-5 end to end: derive issues -> BCF -> report -> propose markers -> four-eyes -> execute."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import pytest_asyncio

import apps.action as action_app
import apps.ingestion as ingestion_app
from archontos.actions.persistence import PostgresActionStore
from archontos.config import get_settings
from archontos.storage.artifacts import LocalArtifactStore
from tests.cad_fixtures import revision_plan
from tests.integration.conftest import count_rows
from tests.integration.test_cad_checks_postgres import _active_rules

pytestmark = pytest.mark.asyncio

ALICE = {"x-api-key": "key-a"}
BOB = {"x-api-key": "key-b"}


@pytest_asyncio.fixture
async def apps(mvp0_db, monkeypatch, tmp_path):
    factory, _schema = mvp0_db
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "postgres")
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "alice:key-a,bob:key-b")
    monkeypatch.setenv("ARCHONTOS_ACTOR_ROLES", "alice:admin,bob:admin")
    get_settings.cache_clear()
    monkeypatch.setattr(ingestion_app, "get_session_factory", lambda: factory)
    monkeypatch.setattr(action_app, "get_session_factory", lambda: factory)
    store = LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(ingestion_app, "_artifact_store", lambda: store)
    ing = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ingestion_app.app), base_url="http://t"
    )
    act = httpx.AsyncClient(transport=httpx.ASGITransport(app=action_app.app), base_url="http://t")
    async with ing, act:
        yield ing, act, factory, store
    get_settings.cache_clear()


async def test_derive_bcf_report_propose_approve_execute(apps, monkeypatch):
    ing, act, factory, store = apps
    up = await ing.post(
        "/v1/cad/artifacts",
        params={"project_id": "P-5", "source_key": "A-901"},
        content=revision_plan(corridor_width=900),
        headers={**ALICE, "content-type": "application/octet-stream"},
    )
    assert up.status_code == 201, up.text
    revision = up.json()["revision"]
    original_sha = revision["sha256"]
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
    run = await ing.post(
        f"/v1/cad/revisions/{revision['id']}/checks",
        json={"jurisdiction_code": "KR", "reference_date": "2026-11-01"},
        headers=ALICE,
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run"]["id"]

    derived = await ing.post(f"/v1/cad/checks/{run_id}/issues", headers=ALICE)
    assert derived.status_code == 200, derived.text
    issues = derived.json()["items"]
    assert issues and all(i["rule_version_id"] == rule_id for i in issues)

    bcf = await ing.get("/v1/cad/issues/export.bcf", params={"run_id": run_id}, headers=ALICE)
    assert bcf.status_code == 200
    assert "bcf.version" in zipfile.ZipFile(io.BytesIO(bcf.content)).namelist()

    rep = await ing.post(
        "/v1/cad/reports",
        json={"run_id": run_id, "generated_at": "2026-11-10T00:00:00+00:00"},
        headers=ALICE,
    )
    assert rep.status_code == 201, rep.text
    report = rep.json()["report"]
    assert rep.json()["content"]["summary"]["FAIL"] == 1
    again = await ing.post(
        "/v1/cad/reports",
        json={"run_id": run_id, "generated_at": "2026-11-10T00:00:00+00:00"},
        headers=ALICE,
    )
    assert again.status_code == 200 and again.json()["report"]["id"] == report["id"]
    html = await ing.get(f"/v1/cad/reports/{report['id']}", params={"format": "html"}, headers=BOB)
    assert html.status_code == 200 and "<!DOCTYPE html>" in html.text
    assert await count_rows(factory, "outbox_message", "topic = 'cad.report-generated'") == 1

    proposed = await ing.post(f"/v1/cad/checks/{run_id}/markers", headers=ALICE)
    assert proposed.status_code == 201, proposed.text
    action = proposed.json()
    assert action["status"] == "proposed" and action["action_type"] == "CadMarkersExport"
    action_id = action["id"]

    # not approved yet -> gate refuses; the generic execute never runs marker actions
    early = await ing.post(f"/v1/cad/markers/{action_id}/execute", headers=BOB)
    assert early.status_code == 409
    generic = await act.post(f"/v1/actions/{action_id}/execute", json={}, headers=BOB)
    assert generic.status_code == 409

    # four-eyes: the proposer cannot approve
    self_ok = await act.post(f"/v1/actions/{action_id}/approve", json={}, headers=ALICE)
    assert self_ok.status_code == 403
    ok = await act.post(f"/v1/actions/{action_id}/approve", json={}, headers=BOB)
    assert ok.status_code == 200, ok.text

    # a failure after the output was written rolls the whole execute back (single transaction)
    async def _boom(self, session, action_id, actor):
        raise RuntimeError("simulated failure after the output link")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresActionStore, "execute_in", _boom)
        with pytest.raises(RuntimeError, match="simulated"):
            await ing.post(f"/v1/cad/markers/{action_id}/execute", headers=BOB)
    assert await count_rows(factory, "cad_issue_artifact", "kind = 'markers'") == 0
    assert await count_rows(factory, "outbox_message", "topic = 'cad.markers-exported'") == 0
    assert await count_rows(factory, "action", f"id = '{action_id}' AND status = 'approved'") == 1

    done = await ing.post(f"/v1/cad/markers/{action_id}/execute", headers=BOB)
    assert done.status_code == 200, done.text
    output = done.json()["output"]
    assert done.json()["action"]["status"] == "succeeded"
    assert output["kind"] == "markers" and output["sha256"] != original_sha
    data = await store.get_bytes(output["storage_uri"])
    assert b"ARCHONTOS-ISSUE" in data
    assert await count_rows(factory, "outbox_message", "topic = 'cad.markers-exported'") == 1
    repeat = await ing.post(f"/v1/cad/markers/{action_id}/execute", headers=BOB)
    assert repeat.status_code == 200 and repeat.json()["created"] is False
    assert await count_rows(factory, "outbox_message", "topic = 'cad.markers-exported'") == 1

    # the original drawing is unchanged
    source = (await ing.get(f"/v1/cad/revisions/{revision['id']}", headers=ALICE)).json()
    assert source["sha256"] == original_sha
