"""MVP-2 CAD checks through the real ingestion app: approved rules -> run -> results (RLS)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import apps.ingestion as ingestion_app
from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.persistence import CanonicalAssertionRepository
from archontos.domain.enums import ReviewStatus
from archontos.normalization.worker import NormalizationOutboxWorker
from archontos.rules.persistence import CanonicalRuleCompilerRepository
from archontos.storage.artifacts import LocalArtifactStore
from tests.cad_fixtures import korean_plan
from tests.integration.conftest import count_rows, persist_law_version

pytestmark = pytest.mark.asyncio
HEADERS = {"content-type": "application/octet-stream"}


@pytest_asyncio.fixture
async def client(mvp0_db, monkeypatch, tmp_path):
    session_factory, _schema = mvp0_db
    monkeypatch.setattr(ingestion_app, "get_session_factory", lambda: session_factory)
    store = LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(ingestion_app, "_artifact_store", lambda: store)
    transport = httpx.ASGITransport(app=ingestion_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http, session_factory, store


async def _active_rules(factory, store, payloads: list[dict]) -> list[str]:
    version = await persist_law_version(
        session_factory=factory,
        store=store,
        mst="998001",
        enforcement_date=date(2026, 1, 1),
        required_count=2,
    )
    assert version.created
    await NormalizationOutboxWorker(session_factory=factory, artifact_store=store).process_one()
    async with factory() as session:
        evidence_id = (
            await session.execute(
                text(
                    "SELECT id FROM evidence_span WHERE source_version_id = :sv "
                    "AND locator_json->>'kind' = 'subparagraph' LIMIT 1"
                ),
                {"sv": version.source_version_id},
            )
        ).scalar_one()
    ids = []
    for payload in payloads:
        async with factory() as session, session.begin():
            candidate = await CanonicalAssertionRepository(session).create_candidate(
                AssertionCandidateCreate(
                    evidence_span_id=evidence_id,
                    natural_language=payload["title"],
                    structured_payload=payload,
                    interpreter_method="human",
                    interpretation_confidence=1.0,
                )
            )
        async with factory() as session, session.begin():
            await CanonicalAssertionRepository(session).review(
                assertion_id=candidate.assertion_id,
                decision=ReviewStatus.APPROVED,
                reviewer_id="integration-test",
                note="mvp2",
            )
        async with factory() as session, session.begin():
            compiled = await CanonicalRuleCompilerRepository(session).compile_approved_assertion(
                candidate.assertion_id
            )
            assert compiled.status == "active"
            ids.append(str(compiled.rule_version_id))
    return ids


async def test_cad_checks_pass_fail_review_and_listing(client, monkeypatch):
    http, factory, store = client
    upload = await http.post(
        "/v1/cad/artifacts",
        params={"project_id": "P-2", "source_key": "A-201"},
        content=korean_plan(),
        headers=HEADERS,
    )
    assert upload.status_code == 201, upload.text
    rid = upload.json()["revision"]["id"]
    pass_id, fail_id, review_id, unit_id = await _active_rules(
        factory,
        store,
        [
            {
                "title": "출입문 폭 0.85m",
                "fact_path": "door.width",
                "operator": ">=",
                "value": 0.85,
                "unit": "m",
            },
            {
                "title": "출입문 폭 1.2m",
                "fact_path": "door.width",
                "operator": ">=",
                "value": 1.2,
                "unit": "m",
            },
            {
                "title": "계단 폭",
                "fact_path": "stair.width",
                "operator": ">=",
                "value": 1.2,
                "unit": "m",
            },
            {
                "title": "출입문 폭 mm",
                "fact_path": "door.width",
                "operator": ">=",
                "value": 900,
                "unit": "mm",
            },
        ],
    )
    url = f"/v1/cad/revisions/{rid}/checks"
    body = {
        "jurisdiction_code": "KR",
        "reference_date": "2026-11-01",
        "land_parcel": {"zone": "제2종일반주거지역", "site_area_m2": 250.0},
    }
    first = await http.post(url, json=body)
    assert first.status_code == 201, first.text
    data = first.json()
    by_rule = {r["rule_version_id"]: r for r in data["results"]}
    assert by_rule[pass_id]["outcome"] == "PASS"
    assert by_rule[fail_id]["outcome"] == "FAIL"
    assert by_rule[review_id]["outcome"] == "REVIEW"
    assert by_rule[unit_id]["outcome"] == "REVIEW"
    failed = by_rule[fail_id]
    assert failed["jurisdiction_code"] == "KR"
    assert failed["effective"]["valid_from"] == "2026-01-01"
    assert failed["evidence_locator"][0]["locator"]["kind"] == "subparagraph"
    assert len(failed["arch_object_ids"]) == 1 and failed["entity_handles"]
    assert data["run"]["fail_count"] == 1 and data["run"]["pass_count"] == 1

    again = await http.post(url, json=body)
    assert again.status_code == 200 and again.json()["run"]["id"] == data["run"]["id"]
    assert await count_rows(factory, "cad_check_run") == 1
    assert await count_rows(factory, "outbox_message", "topic = 'cad.checks-completed'") == 1

    results_url = f"{url}/results"
    fails = (await http.get(results_url, params={"outcome": "FAIL"})).json()["items"]
    assert [r["rule_version_id"] for r in fails] == [fail_id]
    obj = failed["arch_object_ids"][0]
    by_obj = (await http.get(results_url, params={"arch_object_id": obj})).json()["items"]
    assert {r["rule_version_id"] for r in by_obj} == {fail_id, unit_id}
    seen, cursor = [], None
    for _ in range(4):
        page = (
            await http.get(
                results_url, params={"limit": 1, **({"cursor": cursor} if cursor else {})}
            )
        ).json()
        seen += [r["id"] for r in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(set(seen)) == 4 and cursor is None
    bad = await http.get(results_url, params={"outcome": "PASS", "cursor": "x"})
    assert bad.status_code == 400
    assert len((await http.get(url)).json()["items"]) == 1

    for statement in ("UPDATE cad_check_result SET reason = 'x'", "DELETE FROM cad_check_run"):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement))

    future = await http.post(url, json={**body, "reference_date": "2025-01-01"})
    assert future.status_code == 201 and future.json()["results"] == []
    bad_parcel = await http.post(url, json={**body, "land_parcel": {"site_area_m2": -1}})
    assert bad_parcel.status_code == 422

    from archontos.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_jurisdictions", "JP")
    assert (await http.get(results_url)).status_code == 404
    assert (await http.post(url, json=body)).status_code == 403
