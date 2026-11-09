"""MVP-1 CAD upload -> revision/entities/objects, through the real ingestion app and RLS."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import apps.ingestion as ingestion_app
from archontos.integration.ontology import load_facts_export
from archontos.storage.artifacts import LocalArtifactStore
from tests.cad_fixtures import aia_plan, korean_plan
from tests.integration.conftest import count_rows

URL = "/v1/cad/artifacts"
HEADERS = {"content-type": "application/octet-stream"}
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(mvp0_db, monkeypatch, tmp_path):
    session_factory, _schema = mvp0_db
    monkeypatch.setattr(ingestion_app, "get_session_factory", lambda: session_factory)
    store = LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(ingestion_app, "_artifact_store", lambda: store)
    transport = httpx.ASGITransport(app=ingestion_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http, session_factory


async def _upload(http, data, **params):
    query = {"project_id": "P-1", "source_key": "A-101", **params}
    return await http.post(URL, params=query, content=data, headers=HEADERS)


async def test_upload_is_idempotent_and_listable(client):
    http, factory = client
    first = await _upload(http, korean_plan(), revision_label="R1")
    assert first.status_code == 201, first.text
    revision = first.json()["revision"]
    assert revision["object_count"] == 7 and revision["entity_count"] == 9
    again = await _upload(http, korean_plan(), revision_label="other")
    assert again.status_code == 200
    assert again.json()["revision"]["id"] == revision["id"]
    assert again.json()["revision"]["revision_label"] == "R1"
    assert await count_rows(factory, "cad_revision") == 1
    assert await count_rows(factory, "arch_object") == 7
    assert await count_rows(factory, "arch_object_source") == 8
    assert await count_rows(factory, "outbox_message", "topic = 'cad.revision-ingested'") == 1
    assert await count_rows(factory, "audit_log", "action = 'cad.revision.ingest'") == 1

    rid = revision["id"]
    objects_url = f"/v1/cad/revisions/{rid}/objects"
    spaces = (await http.get(objects_url, params={"kind": "space"})).json()
    (space,) = spaces["items"]
    assert space["properties"]["area"] == {"value": 24.0, "unit": "m2"}
    assert space["subject_ref"]["schema"] == "archontos-aec-subject-ref/1"
    assert space["subject_ref"]["object_id"] == space["id"]

    ids, cursor = [], None
    for _ in range(3):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = (await http.get(objects_url, params=params)).json()
        ids += [o["id"] for o in page["items"]]
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == 7 and cursor is None
    first_page = (await http.get(objects_url, params={"limit": 3})).json()
    wrong = await http.get(
        objects_url, params={"kind": "wall", "cursor": first_page["next_cursor"]}
    )
    assert wrong.status_code == 400

    ents = (await http.get(f"/v1/cad/revisions/{rid}/entities", params={"layer": "벽"})).json()
    assert len(ents["items"]) == 4 and ents["items"][0]["bbox"] is not None
    revs = (await http.get("/v1/cad/revisions", params={"project_id": "P-1"})).json()
    assert [r["id"] for r in revs["items"]] == [rid]
    facts = load_facts_export((await http.get(f"/v1/cad/revisions/{rid}/facts")).json())
    assert facts["facts"]["drawing"]["wall_count"] == 4
    assert (await http.get(f"/v1/cad/revisions/{rid}")).json()["sha256"] == revision["sha256"]
    missing = await http.get("/v1/cad/revisions/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


async def test_new_bytes_new_revision_and_bad_input(client):
    http, factory = client
    assert (await _upload(http, korean_plan())).status_code == 201
    assert (await _upload(http, aia_plan())).status_code == 201
    assert await count_rows(factory, "cad_revision") == 2
    assert (await _upload(http, b"garbage")).status_code == 422


async def test_cad_rows_are_append_only(client):
    http, factory = client
    rid = (await _upload(http, korean_plan())).json()["revision"]["id"]
    for statement in (
        "UPDATE arch_object SET layer = 'x'",
        "DELETE FROM cad_entity",
        f"UPDATE cad_revision SET revision_label = 'x' WHERE id = '{rid}'",
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement))


async def test_other_jurisdiction_is_invisible_and_refused(client, monkeypatch):
    http, _factory = client
    rid = (await _upload(http, korean_plan())).json()["revision"]["id"]
    from archontos.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_jurisdictions", "JP")
    assert (await http.get(f"/v1/cad/revisions/{rid}")).status_code == 404
    assert (await http.get("/v1/cad/revisions")).json()["items"] == []
    assert (await _upload(http, aia_plan())).status_code == 403
