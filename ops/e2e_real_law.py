"""Real-law E2E: law.go.kr -> evidence -> assertions -> rules -> DXF checks -> BCF/report.

Run only through Doppler so the law.go.kr OC key never touches disk or the shell history::

    doppler run --project mcp-agent-credentials --config dev -- \
        python ops/e2e_real_law.py --dsn postgresql://postgres:pw@127.0.0.1:55460/postgres

The key is read from ``LAW_OC`` (or ``ARCHONTOS_LAWGO_OC``) and handed to the existing DRF
client via ``ARCHONTOS_LAWGO_OC``. It is never printed; any text that is printed or written is
passed through :func:`_redact` first. Without a key the script prints ``SKIP`` and exits 0, so
it is safe to leave out of CI.

The run uses a throwaway schema in the given PostgreSQL (dropped afterwards unless ``--keep``)
and the real ingestion / rule-engine / action ASGI apps in-process. Steps:

1. ``POST /v1/sources/lawgo/ingest-current`` for 건축법 시행령 and
   건축물의 피난ㆍ방화구조 등의 기준에 관한 규칙 (real DRF fetch, canonical upsert).
2. Normalization outbox worker -> evidence spans with structured locators.
3. Assertion candidates on the real evidence spans of 규칙 제15조 / 제15조의2, proposed by one
   actor and approved by a different actor (the script refuses self-approval).
4. ``POST /v1/rules/compile/{assertion_id}`` -> active rule versions.
5. Upload ``tests.cad_fixtures.real_law_plan`` (1.5 m and 1.0 m corridors, 1.2 m stair),
   run checks, derive issues, export BCF, generate the report.
6. Marker action through the action API with four-eyes (proposer cannot approve).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sys
import zipfile
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SECRET: str = ""


def _redact(value: object) -> str:
    text = str(value)
    if _SECRET:
        text = text.replace(_SECRET, "***")
    return re.sub(r"(OC=)[^&\s'\"]+", r"\1***", text)


def _say(message: object) -> None:
    print(_redact(message), flush=True)


class E2EFailure(RuntimeError):
    pass


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


ENFORCEMENT_DECREE = "건축법 시행령"
EVACUATION_RULE_HINT = ("피난", "방화구조", "규칙")

AUTHOR = "e2e-author"
REVIEWER = "e2e-reviewer"

#: (key, article_no, article_branch_no, keywords, rule payload, expected outcome)
RULES: tuple[dict[str, Any], ...] = (
    {
        "key": "corridor-width",
        "article_no": "15",
        "branch": "2",
        "keywords": ("복도", "1.2미터"),
        "payload": {
            "title": "복도 유효너비 1.2m 이상 (피난방화규칙 제15조의2, 그 밖의 건축물 편복도)",
            "fact_path": "corridor.width",
            "operator": ">=",
            "value": 1.2,
            "unit": "m",
        },
        "expected": "FAIL",
        "expected_objects": {"PASS": 1, "FAIL": 1},
    },
    {
        "key": "stair-width",
        "article_no": "15",
        "branch": "",
        "keywords": ("120센티미터",),
        "payload": {
            "title": "계단 및 계단참 너비 1.2m 이상 (피난방화규칙 제15조제2항)",
            "fact_path": "stair.width",
            "operator": ">=",
            "value": 1.2,
            "unit": "m",
        },
        "expected": "PASS",
        "expected_objects": {"PASS": 1},
    },
    {
        "key": "stair-riser",
        "article_no": "15",
        "branch": "",
        "keywords": ("단높이",),
        "payload": {
            "title": "계단 단높이 0.18m 이하 (피난방화규칙 제15조제2항) - 평면에 단높이 없음",
            "fact_path": "stair.riser_height",
            "operator": "<=",
            "value": 0.18,
            "unit": "m",
        },
        "expected": "REVIEW",
        "expected_objects": None,
    },
)


async def _drf_exact_name(adapter: Any) -> str:
    page = await adapter.search_laws("피난ㆍ방화구조", display=100)
    names = sorted(
        {
            item.law_name
            for item in page.items
            if all(word in item.law_name for word in EVACUATION_RULE_HINT)
        }
    )
    _expect(len(names) == 1, f"expected one 피난ㆍ방화구조 규칙 in DRF search, got {names}")
    return names[0]


async def _pick_span(session: Any, source_version_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT id, locator_json, text_snippet FROM evidence_span "
                "WHERE source_version_id = :sv "
                "AND locator_json->>'article_no' = :no "
                "AND COALESCE(locator_json->>'article_branch_no', '') = :branch"
            ),
            {"sv": source_version_id, "no": spec["article_no"], "branch": spec["branch"]},
        )
    ).all()
    _expect(bool(rows), f"no evidence spans for 제{spec['article_no']}조 {spec['branch']}")
    hits = [r for r in rows if all(k in (r.text_snippet or "") for k in spec["keywords"])]
    if not hits:
        hits = [r for r in rows if r.locator_json.get("kind") == "article"] or list(rows)
    best = min(hits, key=lambda r: len(r.text_snippet or ""))
    return {"id": best.id, "locator": best.locator_json, "snippet": best.text_snippet or ""}


async def _setup_schema(dsn: str) -> tuple[Any, Any, str, str]:
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from archontos.db.migrate import apply_migrations
    from archontos.db.roles import ensure_login
    from archontos.db.session import install_session_context
    from tests.integration.conftest import _with_credentials, sqlalchemy_url

    schema = f"archontos_e2e_{uuid4().hex[:12]}"
    login = f"archontos_e2e_app_{uuid4().hex[:12]}"
    password = uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}", public')
    await apply_migrations(admin, ROOT / "db" / "migrations")
    await ensure_login(admin, login, password)
    engine = create_async_engine(
        sqlalchemy_url(_with_credentials(dsn, login, password)),
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    factory = install_session_context(async_sessionmaker(engine, expire_on_commit=False))
    return admin, engine, factory, schema + "|" + login


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def run(dsn: str, artifacts_dir: Path, keep: bool) -> dict[str, Any]:
    import httpx
    from sqlalchemy import text

    import apps.action as action_app
    import apps.ingestion as ingestion_app
    import apps.rule_engine as rule_app
    from archontos.assertions.contracts import AssertionCandidateCreate
    from archontos.assertions.persistence import CanonicalAssertionRepository
    from archontos.config import get_settings
    from archontos.domain.enums import ReviewStatus
    from archontos.ingestion.adapters import LawGoKrAdapter
    from archontos.normalization.worker import NormalizationOutboxWorker
    from archontos.storage.artifacts import LocalArtifactStore
    from tests.cad_fixtures import real_law_plan

    key_a, key_b = uuid4().hex, uuid4().hex
    os.environ["ARCHONTOS_ACTION_BACKEND"] = "postgres"
    os.environ["ARCHONTOS_API_KEYS"] = f"{AUTHOR}:{key_a},{REVIEWER}:{key_b}"
    os.environ["ARCHONTOS_ACTOR_ROLES"] = f"{AUTHOR}:admin,{REVIEWER}:admin"
    get_settings.cache_clear()
    settings = get_settings()

    admin, engine, factory, names = await _setup_schema(dsn)
    schema, login = names.split("|")
    store = LocalArtifactStore(artifacts_dir)
    for module in (ingestion_app, action_app, rule_app):
        module.get_session_factory = lambda: factory  # type: ignore[attr-defined]
    ingestion_app._artifact_store = lambda: store  # type: ignore[assignment]
    author_h = {"x-api-key": key_a}
    reviewer_h = {"x-api-key": key_b}
    summary: dict[str, Any] = {"laws": [], "rules": [], "checks": {}, "artifacts": {}}

    def client(app: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://e2e", timeout=120
        )

    try:
        async with client(ingestion_app.app) as ing, client(rule_app.app) as rul:
            async with client(action_app.app) as act:
                # 1. real DRF fetch + canonical ingest
                adapter = LawGoKrAdapter(base_url=settings.lawgo_base_url, oc=settings.lawgo_oc)
                rule_law = await _drf_exact_name(adapter)
                versions: dict[str, str] = {}
                for name in (ENFORCEMENT_DECREE, rule_law):
                    resp = await ing.post(
                        "/v1/sources/lawgo/ingest-current", params={"query": name}, headers=author_h
                    )
                    _expect(
                        resp.status_code == 200,
                        f"ingest {name}: HTTP {resp.status_code} {_redact(resp.text)[:300]}",
                    )
                    body = resp.json()
                    versions[name] = body["source_version_id"]
                    summary["laws"].append(
                        {
                            "name": name,
                            "law_id": body["law_id"],
                            "mst": body["mst"],
                            "body_sha256": body["body_sha256"],
                        }
                    )
                    _say(f"ingested {name} law_id={body['law_id']} mst={body['mst']}")

                # 2. normalization -> evidence spans
                worker = NormalizationOutboxWorker(session_factory=factory, artifact_store=store)
                processed = 0
                while (work := await worker.process_one()) is not None:
                    _expect(work.status == "published", f"normalization status {work.status}")
                    processed += 1
                _expect(processed == 2, f"expected 2 normalized versions, got {processed}")
                async with factory() as session:
                    decree48 = (
                        await session.execute(
                            text(
                                "SELECT locator_json FROM evidence_span "
                                "WHERE source_version_id = :sv AND locator_json->>'kind' = "
                                "'article' AND locator_json->>'article_no' = '48' "
                                "AND COALESCE(locator_json->>'article_branch_no','') = ''"
                            ),
                            {"sv": versions[ENFORCEMENT_DECREE]},
                        )
                    ).scalar_one()
                    spans = {
                        spec["key"]: await _pick_span(session, versions[rule_law], spec)
                        for spec in RULES
                    }
                    counts = (
                        await session.execute(
                            text(
                                "SELECT source_version_id::text, count(*) FROM evidence_span "
                                "GROUP BY 1"
                            )
                        )
                    ).all()
                summary["evidence_counts"] = {
                    name: dict(counts).get(sv, 0) for name, sv in versions.items()
                }
                summary["decree_article_48"] = decree48

                # 3. candidates (author) -> approval (different reviewer) -> 4. compile (HTTP)
                _expect(AUTHOR != REVIEWER, "four-eyes requires two distinct actors")
                rule_ids: dict[str, str] = {}
                for spec in RULES:
                    span = spans[spec["key"]]
                    async with factory() as session, session.begin():
                        await session.execute(
                            text("SELECT set_config('app.actor', :a, true)"), {"a": AUTHOR}
                        )
                        cand = await CanonicalAssertionRepository(session).create_candidate(
                            AssertionCandidateCreate(
                                evidence_span_id=span["id"],
                                natural_language=spec["payload"]["title"],
                                structured_payload=spec["payload"],
                                interpreter_method="human",
                                interpretation_confidence=1.0,
                            )
                        )
                    early = await rul.post(
                        f"/v1/rules/compile/{cand.assertion_id}", headers=author_h
                    )
                    _expect(early.status_code == 409, f"unapproved compile: {early.status_code}")
                    async with factory() as session, session.begin():
                        await session.execute(
                            text("SELECT set_config('app.actor', :a, true)"), {"a": REVIEWER}
                        )
                        review = await CanonicalAssertionRepository(session).review(
                            assertion_id=cand.assertion_id,
                            decision=ReviewStatus.APPROVED,
                            reviewer_id=REVIEWER,
                            note=f"four-eyes: proposed by {AUTHOR}",
                        )
                    _expect(review.reviewer_id != AUTHOR, "self-approval")
                    compiled = await rul.post(
                        f"/v1/rules/compile/{cand.assertion_id}", headers=reviewer_h
                    )
                    _expect(compiled.status_code == 200, f"compile: {_redact(compiled.text)}")
                    cbody = compiled.json()
                    _expect(cbody["status"] == "active", f"rule status {cbody['status']}")
                    rule_ids[spec["key"]] = cbody["rule_version_id"]
                    summary["rules"].append(
                        {
                            "key": spec["key"],
                            "rule_version_id": cbody["rule_version_id"],
                            "assertion_id": str(cand.assertion_id),
                            "proposed_by": AUTHOR,
                            "approved_by": REVIEWER,
                            "locator": span["locator"],
                        }
                    )

                # 5. upload drawing -> checks -> issues -> BCF -> report
                dxf = real_law_plan()
                up = await ing.post(
                    "/v1/cad/artifacts",
                    params={"project_id": "E2E-REAL-LAW", "source_key": "A-101"},
                    content=dxf,
                    headers={**author_h, "content-type": "application/octet-stream"},
                )
                _expect(up.status_code == 201, f"upload: {up.status_code} {up.text[:300]}")
                revision_id = up.json()["revision"]["id"]
                summary["artifacts"]["dxf_sha256"] = _sha(dxf)
                check = await ing.post(
                    f"/v1/cad/revisions/{revision_id}/checks",
                    json={"jurisdiction_code": "KR", "reference_date": date.today().isoformat()},
                    headers=author_h,
                )
                _expect(check.status_code == 201, f"checks: {check.status_code} {check.text[:300]}")
                run_body = check.json()
                run_id = run_body["run"]["id"]
                by_rule = {r["rule_version_id"]: r for r in run_body["results"]}
                for spec in RULES:
                    rid = rule_ids[spec["key"]]
                    result = by_rule.get(rid)
                    if result is None:
                        raise E2EFailure(f"no result for {spec['key']}")
                    locs = [e["locator"] for e in result.get("evidence_locator") or []]
                    expected_loc = spans[spec["key"]]["locator"]
                    _expect(
                        any(loc == expected_loc for loc in locs),
                        f"{spec['key']}: evidence locator missing ({locs})",
                    )
                    _expect(
                        result["outcome"] == spec["expected"],
                        f"{spec['key']}: outcome {result['outcome']} != {spec['expected']}",
                    )
                    objects = (result.get("details") or {}).get("object_outcomes")
                    if spec["expected_objects"]:
                        got = {k: v for k, v in (objects or {}).items() if v}
                        _expect(
                            got == spec["expected_objects"],
                            f"{spec['key']}: object outcomes {got}",
                        )
                    summary["checks"][spec["key"]] = {
                        "outcome": result["outcome"],
                        "object_outcomes": objects,
                        "reason": result.get("reason"),
                        "evidence_locator": expected_loc,
                    }

                issues = await ing.post(f"/v1/cad/checks/{run_id}/issues", headers=author_h)
                _expect(issues.status_code == 200, f"issues: {issues.status_code}")
                items = issues.json()["items"]
                summary["issue_count"] = len(items)
                _expect(
                    {i["rule_version_id"] for i in items}
                    <= {rule_ids["corridor-width"], rule_ids["stair-riser"]},
                    "issues for a passing rule",
                )
                bcf = await ing.get(
                    "/v1/cad/issues/export.bcf", params={"run_id": run_id}, headers=author_h
                )
                _expect(bcf.status_code == 200, f"bcf: {bcf.status_code}")
                bcf_names = zipfile.ZipFile(io.BytesIO(bcf.content)).namelist()
                _expect("bcf.version" in bcf_names, "bcf.version missing")
                summary["artifacts"]["bcf_sha256"] = _sha(bcf.content)
                summary["artifacts"]["bcf_entries"] = len(bcf_names)
                rep = await ing.post(
                    "/v1/cad/reports",
                    json={"run_id": run_id, "generated_at": "2026-11-14T00:00:00+00:00"},
                    headers=author_h,
                )
                _expect(rep.status_code in (200, 201), f"report: {rep.status_code} {rep.text}")
                content = rep.json()["content"]
                summary["report_summary"] = content.get("summary")
                report_id = rep.json()["report"]["id"]
                html = await ing.get(
                    f"/v1/cad/reports/{report_id}", params={"format": "html"}, headers=reviewer_h
                )
                _expect(html.status_code == 200, f"report html: {html.status_code}")
                summary["artifacts"]["report_html_sha256"] = _sha(html.content)
                rendered = html.text
                summary["report_mentions_rule_law"] = rule_law in rendered or "제15조" in rendered

                # 6. marker action with four-eyes over the action API
                proposed = await ing.post(f"/v1/cad/checks/{run_id}/markers", headers=author_h)
                _expect(proposed.status_code == 201, f"markers propose: {proposed.status_code}")
                action_id = proposed.json()["id"]
                self_ok = await act.post(
                    f"/v1/actions/{action_id}/approve", json={}, headers=author_h
                )
                _expect(self_ok.status_code == 403, f"self-approve: {self_ok.status_code}")
                ok = await act.post(f"/v1/actions/{action_id}/approve", json={}, headers=reviewer_h)
                _expect(ok.status_code == 200, f"approve: {ok.status_code}")
                done = await ing.post(f"/v1/cad/markers/{action_id}/execute", headers=reviewer_h)
                _expect(done.status_code == 200, f"execute: {done.status_code}")
                summary["artifacts"]["markers_sha256"] = done.json()["output"]["sha256"]
                summary["four_eyes"] = {
                    "self_approve_status": self_ok.status_code,
                    "other_approve_status": ok.status_code,
                }
    finally:
        await engine.dispose()
        if not keep:
            await admin.execute("SET search_path TO public")
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.execute(f'DROP ROLE IF EXISTS "{login}"')
        await admin.close()
    return summary


def main() -> int:
    global _SECRET
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.getenv("ARCHONTOS_TEST_DATABASE_URL"))
    parser.add_argument("--artifacts", default=None, help="artifact dir (default: temp)")
    parser.add_argument("--summary-json", default=None, help="write the sanitized summary here")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway schema")
    args = parser.parse_args()

    key = (os.getenv("LAW_OC") or os.getenv("ARCHONTOS_LAWGO_OC") or "").strip()
    if not key:
        _say("SKIP: LAW_OC / ARCHONTOS_LAWGO_OC is not set (run through doppler run)")
        return 0
    if not args.dsn:
        _say("SKIP: no --dsn / ARCHONTOS_TEST_DATABASE_URL")
        return 0
    _SECRET = key
    os.environ["ARCHONTOS_LAWGO_OC"] = key
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    import tempfile

    dsn = args.dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    with tempfile.TemporaryDirectory(prefix="archontos-e2e-") as tmp:
        artifacts = Path(args.artifacts) if args.artifacts else Path(tmp)
        try:
            summary = asyncio.run(run(dsn, artifacts, args.keep))
        except Exception as exc:  # noqa: BLE001 - report every failure redacted
            _say(f"E2E FAILED: {type(exc).__name__}: {exc}")
            if "HTTP error 302" in str(exc):
                _say(
                    "hint: law.go.kr DRF redirected to /sorry/sorry.html - the OC key is not "
                    "accepted from this caller (IP/domain registration) or the service is paused"
                )
            return 1
    text = _redact(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if args.summary_json:
        Path(args.summary_json).write_text(text + "\n", encoding="utf-8")
    _say(text)
    _say("E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
