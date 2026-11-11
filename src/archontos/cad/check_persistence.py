"""PostgreSQL side of MVP-2 CAD checks (migration 015): load bound rules, persist, list."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.checks import (
    CHECKER_VERSION,
    FACT_KINDS,
    CheckResult,
    CheckRule,
    CheckRun,
    extract_fact_space,
    run_checks,
)
from archontos.cad.dxf import CAD_NAMESPACE, canonical_json, sha256_hex
from archontos.cad.persistence import decode_cursor, encode_cursor, filter_fingerprint

_RULES_SQL = """
    SELECT rv.id::text AS id, rv.logic_expr, rv.status, rv.valid_from, rv.valid_to,
           rv.binding, r.title, sd.jurisdiction_code,
           COALESCE(bool_and(a.review_status = 'approved')
                    FILTER (WHERE ra.assertion_id IS NOT NULL), false) AS basis_approved,
           COALESCE(jsonb_agg(DISTINCT jsonb_build_object(
                        'evidence_span_id', es.id::text,
                        'source_version_id', es.source_version_id::text,
                        'locator', es.locator_json))
                    FILTER (WHERE es.id IS NOT NULL), '[]'::jsonb) AS evidence
      FROM rule_version rv
      JOIN rule r ON r.id = rv.rule_id
      JOIN source_version sv ON sv.id = r.source_version_id
      JOIN source_document sd ON sd.id = sv.source_id
      LEFT JOIN rule_assertion ra ON ra.rule_version_id = rv.id AND ra.role = 'basis'
      LEFT JOIN assertion a ON a.id = ra.assertion_id
      LEFT JOIN evidence_span es ON es.id = a.evidence_span_id
     WHERE rv.status = 'active'
       AND rv.valid_from <= :ref
       AND (rv.valid_to IS NULL OR rv.valid_to >= :ref)
       AND (CAST(:ids AS uuid[]) IS NULL OR rv.id = ANY(CAST(:ids AS uuid[])))
     GROUP BY rv.id, r.title, sd.jurisdiction_code
     ORDER BY rv.id
"""


#: Keys of a stored run's ``facts_json`` that come from the drawing/run, not from the caller.
_DERIVED_FACT_ROOTS = frozenset({"drawing", "context", "fact_units", "land_parcel", *FACT_KINDS})


def caller_inputs(run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Recover the caller ``facts`` and ``fact_units`` of a stored run (for a re-run)."""
    facts = {k: v for k, v in (run["facts"] or {}).items() if k not in _DERIVED_FACT_ROOTS}
    units = {p: u for p, u in (run["fact_units"] or {}).items() if p.split(".", 1)[0] in facts}
    return facts, units


@dataclass(frozen=True, slots=True)
class PersistedCheckRun:
    run: dict[str, Any]
    created: bool


def run_id_for(
    revision_id: str,
    *,
    jurisdiction: str,
    reference_date: date,
    inputs_digest: str,
) -> str:
    return str(
        uuid.uuid5(
            CAD_NAMESPACE,
            f"check\n{revision_id}\n{jurisdiction}\n{reference_date.isoformat()}\n"
            f"{inputs_digest}\n{CHECKER_VERSION}",
        )
    )


def inputs_digest(
    *, land_parcel: dict[str, Any], facts: dict[str, Any], rules: list[CheckRule]
) -> str:
    """Digest of the caller inputs and the exact rule logic that was bound."""
    return sha256_hex(
        canonical_json(
            {
                "land_parcel": land_parcel,
                "facts": facts,
                "rules": [
                    {"id": r.rule_version_id, "logic": sha256_hex(canonical_json(r.logic_expr))}
                    for r in sorted(rules, key=lambda r: r.rule_version_id)
                ],
            }
        )
    )


def result_view(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("valid_from", "valid_to"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    out["arch_object_ids"] = [str(x) for x in out.get("arch_object_ids") or []]
    out["effective"] = {"valid_from": out.pop("valid_from"), "valid_to": out.pop("valid_to")}
    return out


#: Diffs that supersede a revision's checks: the revision is the base of a lineage diff to a
#: revision created no earlier, with at least one added/removed/modified object. Check runs
#: and results of such a revision are stale (docs/MVP3-REVISION-DIFF.md).

_RUN_SELECT = """
    SELECT c.id::text AS id, c.revision_id::text AS revision_id, c.jurisdiction_code,
           c.reference_date, c.land_parcel_json AS land_parcel, c.inputs_digest,
           c.checker_version, c.rule_count, c.pass_count, c.fail_count, c.review_count,
           c.not_applicable_count, c.created_by, c.created_at,
           ARRAY(SELECT d.id::text FROM cad_revision_diff d
      JOIN cad_revision tr ON tr.id = d.target_revision_id
      JOIN cad_revision br ON br.id = d.base_revision_id
     WHERE d.base_revision_id = c.revision_id AND d.relation = 'lineage'
       AND tr.created_at >= br.created_at
       AND d.added_count + d.removed_count + d.modified_count > 0
     ORDER BY d.id) AS stale_by_diff_ids
      FROM cad_check_run c
"""


def _run_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    out["reference_date"] = out["reference_date"].isoformat()
    out["created_at"] = out["created_at"].isoformat()
    out["stale"] = bool(out["stale_by_diff_ids"])
    return out


class CadCheckRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def load_rules(
        self, *, reference_date: date, rule_version_ids: list[str] | None
    ) -> list[CheckRule]:
        rows = (
            await self.session.execute(
                text(_RULES_SQL), {"ref": reference_date, "ids": rule_version_ids}
            )
        ).all()
        return [
            CheckRule(
                rule_version_id=row.id,
                title=row.title,
                logic_expr=dict(row.logic_expr),
                jurisdiction_code=row.jurisdiction_code,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                status=row.status,
                binding=bool(row.binding),
                basis_approved=bool(row.basis_approved),
                evidence_locators=tuple(row.evidence or ()),
            )
            for row in rows
        ]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(_RUN_SELECT + " WHERE c.id = CAST(:id AS uuid)"), {"id": run_id}
            )
        ).first()
        return _run_row(row) if row else None

    async def run_inputs(self, run_id: str) -> dict[str, Any]:
        row = (
            await self.session.execute(
                text(
                    "SELECT jurisdiction_code, reference_date, land_parcel_json AS land_parcel, "
                    "facts_json AS facts, fact_units_json AS fact_units "
                    "FROM cad_check_run WHERE id = CAST(:id AS uuid)"
                ),
                {"id": run_id},
            )
        ).one()
        return dict(row._mapping)

    async def all_results(self, revision_id: str, run_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            page, cursor = await self.list_results(
                revision_id,
                run_id=run_id,
                outcome=None,
                rule_version_id=None,
                arch_object_id=None,
                limit=500,
                cursor=cursor,
            )
            out.extend(page)
            if cursor is None:
                return out

    async def evaluate(
        self,
        *,
        revision_id: str,
        objects: list[dict[str, Any]],
        jurisdiction_code: str,
        reference_date: date,
        land_parcel: dict[str, Any],
        facts: dict[str, Any],
        fact_units: dict[str, str],
        rule_version_ids: list[str] | None,
        model_meta: dict[str, Any] | None = None,
    ) -> tuple[PersistedCheckRun, CheckRun]:
        """Bind rules, run them on ``objects`` and persist (idempotent). May raise
        ``CadCheckInputError``."""
        rules = await self.load_rules(
            reference_date=reference_date, rule_version_ids=rule_version_ids
        )
        run = run_checks(
            rules,
            extract_fact_space(objects, model_meta=model_meta),
            jurisdiction=jurisdiction_code,
            reference_date=reference_date,
            land_parcel=land_parcel,
            extra_facts=facts,
            extra_fact_units=fact_units,
        )
        digest = inputs_digest(
            land_parcel=land_parcel,
            facts={"facts": facts, "fact_units": fact_units},
            rules=[r.rule for r in run.results],
        )
        persisted = await self.persist(
            revision_id=revision_id, run=run, land_parcel=land_parcel, digest=digest
        )
        return persisted, run

    async def list_runs(self, revision_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                text(
                    _RUN_SELECT
                    + " WHERE c.revision_id = CAST(:rev AS uuid) ORDER BY c.created_at, c.id"
                ),
                {"rev": revision_id},
            )
        ).all()
        return [_run_row(r) for r in rows]

    async def persist(
        self,
        *,
        revision_id: str,
        run: CheckRun,
        land_parcel: dict[str, Any],
        digest: str,
    ) -> PersistedCheckRun:
        run_id = run_id_for(
            revision_id,
            jurisdiction=run.jurisdiction,
            reference_date=run.reference_date,
            inputs_digest=digest,
        )
        existing = await self.get_run(run_id)
        if existing is not None:
            return PersistedCheckRun(existing, False)
        decided = [r for r in run.results if r.applicable and r.outcome is not None]
        summary = run.summary()
        await self.session.execute(
            text(
                """
                INSERT INTO cad_check_run(id, revision_id, jurisdiction_code, reference_date,
                    land_parcel_json, facts_json, fact_units_json, inputs_digest,
                    checker_version, rule_count, pass_count, fail_count, review_count,
                    not_applicable_count)
                VALUES (CAST(:id AS uuid), CAST(:rev AS uuid), :jur, :ref,
                    CAST(:parcel AS jsonb), CAST(:facts AS jsonb), CAST(:units AS jsonb),
                    :digest, :ver, :rc, :pc, :fc, :vc, :nc)
                """
            ),
            {
                "id": run_id,
                "rev": revision_id,
                "jur": run.jurisdiction,
                "ref": run.reference_date,
                "parcel": json.dumps(land_parcel, ensure_ascii=False),
                "facts": json.dumps(run.facts, ensure_ascii=False, sort_keys=True),
                "units": json.dumps(run.fact_units, sort_keys=True),
                "digest": digest,
                "ver": CHECKER_VERSION,
                "rc": summary["rule_count"],
                "pc": summary["pass_count"],
                "fc": summary["fail_count"],
                "vc": summary["review_count"],
                "nc": summary["not_applicable_count"],
            },
        )
        if decided:
            await self.session.execute(
                text(
                    """
                    INSERT INTO cad_check_result(id, run_id, rule_version_id, rule_title,
                        outcome, reason, details_json, jurisdiction_code, valid_from, valid_to,
                        binding, evidence_locator_json, fact_paths, arch_object_ids,
                        entity_handles)
                    VALUES (CAST(:id AS uuid), CAST(:run AS uuid), CAST(:rv AS uuid), :title,
                        :outcome, :reason, CAST(:details AS jsonb), :jur, :vf, :vt, :binding,
                        CAST(:evidence AS jsonb), :paths, CAST(:objects AS uuid[]), :handles)
                    """
                ),
                [self._result_params(run_id, r) for r in decided],
            )
        payload = {
            "cad_check_run_id": run_id,
            "cad_revision_id": revision_id,
            "jurisdiction_code": run.jurisdiction,
            "reference_date": run.reference_date.isoformat(),
            **summary,
        }
        event_id: Any = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO domain_event(aggregate_type, aggregate_id, event_type,
                                             payload_json)
                    VALUES ('cad_check_run', :agg, 'CadChecksCompleted',
                            CAST(:payload AS jsonb))
                    RETURNING event_id
                    """
                ),
                {"agg": run_id, "payload": json.dumps(payload)},
            )
        ).scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (:event_id, 'cad.checks-completed', CAST(:payload AS jsonb))
                """
            ),
            {"event_id": event_id, "payload": json.dumps(payload)},
        )
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.checks.run', 'cad_check_run', :id, CAST(:after AS jsonb))
                """
            ),
            {"id": run_id, "after": json.dumps(payload, sort_keys=True)},
        )
        created = await self.get_run(run_id)
        assert created is not None  # noqa: S101 - inserted in this transaction
        return PersistedCheckRun(created, True)

    @staticmethod
    def _result_params(run_id: str, result: CheckResult) -> dict[str, Any]:
        rule = result.rule
        assert result.outcome is not None  # noqa: S101 - filtered by caller
        return {
            "id": str(uuid.uuid5(CAD_NAMESPACE, f"result\n{run_id}\n{rule.rule_version_id}")),
            "run": run_id,
            "rv": rule.rule_version_id,
            "title": rule.title,
            "outcome": result.outcome.value,
            "reason": result.reason,
            "details": json.dumps(result.details, ensure_ascii=False, default=str),
            "jur": rule.jurisdiction_code,
            "vf": rule.valid_from,
            "vt": rule.valid_to,
            "binding": rule.binding,
            "evidence": json.dumps(list(rule.evidence_locators), ensure_ascii=False),
            "paths": list(result.fact_paths),
            "objects": list(result.object_ids),
            "handles": list(result.handles),
        }

    async def list_results(
        self,
        revision_id: str,
        *,
        run_id: str | None,
        outcome: str | None,
        rule_version_id: str | None,
        arch_object_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(
            r=revision_id, run=run_id, o=outcome, rv=rule_version_id, obj=arch_object_id
        )
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT x.id::text AS id, x.run_id::text AS run_id,
                           x.rule_version_id::text AS rule_version_id, x.rule_title,
                           x.outcome, x.reason, x.details_json AS details,
                           x.jurisdiction_code, x.valid_from, x.valid_to, x.binding,
                           x.evidence_locator_json AS evidence_locator, x.fact_paths,
                           x.arch_object_ids, x.entity_handles, c.reference_date,
                           ARRAY(SELECT d.id::text FROM cad_revision_diff d
      JOIN cad_revision tr ON tr.id = d.target_revision_id
      JOIN cad_revision br ON br.id = d.base_revision_id
     WHERE d.base_revision_id = c.revision_id AND d.relation = 'lineage'
       AND tr.created_at >= br.created_at
       AND d.added_count + d.removed_count + d.modified_count > 0
     ORDER BY d.id) AS stale_by_diff_ids
                      FROM cad_check_result x
                      JOIN cad_check_run c ON c.id = x.run_id
                     WHERE c.revision_id = CAST(:rev AS uuid)
                       AND (CAST(:run AS uuid) IS NULL OR x.run_id = CAST(:run AS uuid))
                       AND (CAST(:o AS text) IS NULL OR x.outcome = :o)
                       AND (CAST(:rv AS uuid) IS NULL OR x.rule_version_id = CAST(:rv AS uuid))
                       AND (CAST(:obj AS uuid) IS NULL
                            OR CAST(:obj AS uuid) = ANY(x.arch_object_ids))
                       AND (CAST(:after AS uuid) IS NULL OR x.id > CAST(:after AS uuid))
                     ORDER BY x.id LIMIT :lim
                    """
                ),
                {
                    "rev": revision_id,
                    "run": run_id,
                    "o": outcome,
                    "rv": rule_version_id,
                    "obj": arch_object_id,
                    "after": after,
                    "lim": limit + 1,
                },
            )
        ).all()
        items = []
        for row in rows[:limit]:
            item = result_view(dict(row._mapping))
            item["reference_date"] = item["reference_date"].isoformat()
            item["stale"] = bool(item["stale_by_diff_ids"])
            items.append(item)
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt
