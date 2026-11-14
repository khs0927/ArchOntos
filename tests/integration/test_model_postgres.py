"""MVP-4 through the real ingestion app: SketchUp JSON / IFC upload, links, checks, diffs."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import apps.ingestion as ingestion_app
from archontos.integration.ontology import load_facts_export
from archontos.storage.artifacts import LocalArtifactStore
from tests.integration.conftest import count_rows
from tests.integration.test_cad_checks_postgres import _active_rules
from tests.model_fixtures import DOOR_GUID, skp_bytes

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


async def _upload(http, data: bytes, source_key: str, label: str, ctype: str):
    return await http.post(
        "/v1/cad/artifacts",
        params={"project_id": "P-4", "source_key": source_key, "revision_label": label},
        content=data,
        headers={"content-type": ctype},
    )


async def test_skp_upload_checks_and_diff(client):
    http, factory, store = client
    first = await _upload(http, skp_bytes(), "M-1", "R1", "application/json")
    assert first.status_code == 201, first.text
    rev = first.json()["revision"]
    assert rev["source_format"] == "skp_json" and rev["object_count"] == 4
    assert rev["dxf_version"] == "archontos-skp-export/1"
    again = await _upload(http, skp_bytes(), "M-1", "R1", "application/octet-stream")
    assert again.status_code == 200 and again.json()["revision"]["id"] == rev["id"]
    assert await count_rows(factory, "arch_object_source_ref") == 5  # 4 pids + 1 ifc_guid

    doors = await http.get(f"/v1/cad/revisions/{rev['id']}/objects", params={"kind": "door"})
    (door,) = doors.json()["items"]
    assert door["properties"]["width"] == {"value": 0.9, "unit": "m"}
    assert door["source_handles"] == ["101"]

    facts = (await http.get(f"/v1/cad/revisions/{rev['id']}/facts")).json()
    loaded = load_facts_export(facts)
    assert loaded["facts"]["building"]["floor_count"] == 1
    assert loaded["facts"]["drawing"]["door_count"] == 1

    (rule_id,) = await _active_rules(
        factory,
        store,
        [
            {
                "title": "문 폭 0.9m",
                "fact_path": "door.width",
                "operator": ">=",
                "value": 0.9,
                "unit": "m",
            }
        ],
    )
    checks = await http.post(
        f"/v1/cad/revisions/{rev['id']}/checks",
        json={"jurisdiction_code": "KR", "reference_date": "2026-11-01"},
    )
    assert checks.status_code == 201, checks.text
    assert [r["outcome"] for r in checks.json()["results"]] == ["PASS"]
    clash = await http.post(
        f"/v1/cad/revisions/{rev['id']}/checks",
        json={"jurisdiction_code": "KR", "facts": {"building": {"floor_count": 9}}},
    )
    assert clash.status_code == 422

    second = await _upload(http, skp_bytes(door_width_mm=800), "M-1", "R2", "application/json")
    assert second.status_code == 201, second.text
    body = {
        "base_revision_id": rev["id"],
        "target_revision_id": second.json()["revision"]["id"],
        "rerun_checks": True,
    }
    diff = await http.post("/v1/cad/diffs", json=body)
    assert diff.status_code == 201, diff.text
    payload = diff.json()["diff"]
    assert payload["modified_count"] == 1 and payload["added_count"] == 0
    (transition,) = [t for t in payload["transitions"] if t["rule_version_id"] == rule_id]
    assert transition["base_outcome"] == "PASS" and transition["target_outcome"] == "FAIL"


async def test_binary_skp_and_bad_json_are_refused(client):
    http, _factory, _store = client
    binary = b"\xff\xfe\xff\x0eS\x00k\x00e\x00t\x00c\x00h\x00U\x00p\x00 Model"
    res = await _upload(http, binary, "M-2", "R1", "application/octet-stream")
    assert res.status_code == 415
    bad = await _upload(http, b'{"schema": "nope"}', "M-2", "R1", "application/json")
    assert bad.status_code == 422


async def test_ifc_and_skp_are_linked_by_guid(client):
    pytest.importorskip("ifcopenshell")
    from tests.model_fixtures import tiny_ifc

    http, factory, _store = client
    ifc = await _upload(http, tiny_ifc(), "M-IFC", "R1", "application/x-step")
    assert ifc.status_code == 201, ifc.text
    ifc_rev = ifc.json()["revision"]
    assert ifc_rev["source_format"] == "ifc" and ifc_rev["model_meta"]["schema"] == "IFC4"
    facts = load_facts_export((await http.get(f"/v1/cad/revisions/{ifc_rev['id']}/facts")).json())
    assert facts["facts"]["building"] == {"storey_count": 3, "floor_count": 2, "basement_count": 1}

    skp = await _upload(http, skp_bytes(), "M-SKP", "R1", "application/json")
    assert skp.status_code == 201, skp.text
    skp_rev = skp.json()["revision"]["id"]
    links = (await http.get("/v1/models/links", params={"project_id": "P-4"})).json()
    (link,) = links["items"]
    assert link["method"] == "shared_ref" and link["ref_value"] == DOOR_GUID
    assert {link["object_a_source_format"], link["object_b_source_format"]} == {"ifc", "skp_json"}
    assert {link["revision_a_id"], link["revision_b_id"]} == {ifc_rev["id"], skp_rev}

    # A SketchUp export of another revision label is not linked automatically.
    await _upload(http, skp_bytes(door_width_mm=850), "M-SKP", "R2", "application/json")
    assert await count_rows(factory, "model_object_link") == 1

    walls_ifc = (
        await http.get(f"/v1/cad/revisions/{ifc_rev['id']}/objects", params={"kind": "wall"})
    ).json()["items"]
    walls_skp = (
        await http.get(f"/v1/cad/revisions/{skp_rev}/objects", params={"kind": "wall"})
    ).json()["items"]
    body = {"object_a_id": walls_ifc[0]["id"], "object_b_id": walls_skp[0]["id"]}
    manual = await http.post("/v1/models/links", json=body)
    assert manual.status_code == 201, manual.text
    assert manual.json()["link"]["method"] == "manual"
    swapped = {"object_a_id": body["object_b_id"], "object_b_id": body["object_a_id"]}
    assert (await http.post("/v1/models/links", json=swapped)).status_code == 200
    same_rev = {"object_a_id": walls_ifc[0]["id"], "object_b_id": walls_ifc[0]["id"]}
    assert (await http.post("/v1/models/links", json=same_rev)).status_code == 422
    page = (await http.get("/v1/models/links", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = (
        await http.get("/v1/models/links", params={"limit": 1, "cursor": page["next_cursor"]})
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None

    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE model_object_link SET note = 'x'"))


async def test_dxf_and_ifc_are_linked_by_geometry(client):
    """P2 / migration 022: same project + revision label, DXF <-> IFC by kind and overlap."""
    pytest.importorskip("ifcopenshell")
    pytest.importorskip("ezdxf")
    from tests.cad_fixtures import korean_plan
    from tests.model_fixtures import tiny_ifc

    http, factory, _store = client
    ifc = await _upload(http, tiny_ifc(), "G-IFC", "G1", "application/x-step")
    assert ifc.status_code == 201, ifc.text
    dxf = await _upload(http, korean_plan(), "G-DXF", "G1", "application/octet-stream")
    assert dxf.status_code == 201, dxf.text
    links = (
        await http.get("/v1/models/links", params={"project_id": "P-4", "method": "geometric"})
    ).json()["items"]
    kinds = sorted(link["object_a_kind"] for link in links)
    assert "door" in kinds and "space" in kinds
    for link in links:
        assert link["object_a_kind"] == link["object_b_kind"]
        assert 0.5 <= link["confidence"] <= 1.0
        assert {link["object_a_source_format"], link["object_b_source_format"]} == {"dxf", "ifc"}
    # A different revision label is not linked; geometric rows obey the same append-only rule.
    await _upload(http, korean_plan(room_name="침실"), "G-DXF", "G2", "application/octet-stream")
    assert await count_rows(factory, "model_object_link", "method = 'geometric'") == len(links)
