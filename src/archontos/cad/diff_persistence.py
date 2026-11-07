"""PostgreSQL side of MVP-3 revision diffs (migration 017): compute, persist, read."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.check_persistence import CadCheckRepository, caller_inputs
from archontos.cad.checks import CadCheckInputError
from archontos.cad.diff import (
    DIFFER_VERSION,
    DiffInputError,
    DiffTolerances,
    attach_impact,
    change_id,
    diff_objects,
    summarize,
    transitions,
)
from archontos.cad.dxf import CAD_NAMESPACE, canonical_json, sha256_hex
from archontos.cad.persistence import (
    CadRepository,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)


class RevisionNotFound(LookupError):
    """A revision does not exist or is outside the caller's jurisdictions (HTTP 404)."""


@dataclass(frozen=True, slots=True)
class PersistedDiff:
    diff: dict[str, Any]
    created: bool


def diff_id_for(base: str, target: str, tolerances_digest: str, rerun: bool) -> str:
    return str(
        uuid.uuid5(
            CAD_NAMESPACE,
            f"diff\n{base}\n{target}\n{tolerances_digest}\n{int(rerun)}\n{DIFFER_VERSION}",
        )
    )


_DIFF_SELECT = """
    SELECT d.id::text AS id, d.base_revision_id::text AS base_revision_id,
           d.target_revision_id::text AS target_revision_id, d.relation, d.jurisdiction_code,
           d.tolerances_json AS tolerances, d.rerun_checks, d.differ_version,
           d.base_object_count, d.target_object_count, d.added_count, d.removed_count,
           d.modified_count, d.unchanged_count, d.summary_json AS summary,
           d.transitions_json AS transitions, d.stale_json AS stale, d.created_by, d.created_at
      FROM cad_revision_diff d
"""


def _diff_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    out["created_at"] = out["created_at"].isoformat()
    return out


class CadDiffRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.cad = CadRepository(session)
        self.checks = CadCheckRepository(session)

    async def get_diff(self, diff_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(_DIFF_SELECT + " WHERE d.id = CAST(:id AS uuid)"), {"id": diff_id}
            )
        ).first()
        return _diff_row(row) if row else None

    async def create(
        self,
        *,
        base_revision_id: str,
        target_revision_id: str,
        tolerances: DiffTolerances,
        rerun_checks: bool,
    ) -> PersistedDiff:
        if base_revision_id == target_revision_id:
            raise DiffInputError("base and target revision must differ")
        base = await self.cad.get_revision(base_revision_id)
        target = await self.cad.get_revision(target_revision_id)
        if base is None or target is None:
            raise RevisionNotFound("CAD revision not found")
        if base["jurisdiction_code"] != target["jurisdiction_code"]:
            raise DiffInputError("revisions belong to different jurisdictions")
        tol_digest = sha256_hex(canonical_json(tolerances.as_dict()))
        diff_id = diff_id_for(base_revision_id, target_revision_id, tol_digest, rerun_checks)
        existing = await self.get_diff(diff_id)
        if existing is not None:
            return PersistedDiff(existing, False)

        base_objects = await self.cad.all_objects(base_revision_id)
        target_objects = await self.cad.all_objects(target_revision_id)
        changes = diff_objects(base_objects, target_objects, tolerances)
        for c in changes:
            c["id"] = change_id(diff_id, c, CAD_NAMESPACE)

        base_runs = await self.checks.list_runs(base_revision_id)
        target_runs = await self.checks.list_runs(target_revision_id)
        base_results: list[dict[str, Any]] = []
        results_by_run: dict[str, list[dict[str, Any]]] = {}
        for run in base_runs:
            results_by_run[run["id"]] = await self.checks.all_results(base_revision_id, run["id"])
            base_results += results_by_run[run["id"]]

        pairs: list[dict[str, Any]] = []
        target_results: list[dict[str, Any]] = []
        rerun_results: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
        if rerun_checks:
            for run in base_runs:
                rule_ids = sorted({r["rule_version_id"] for r in results_by_run[run["id"]]})
                if not rule_ids:
                    continue
                inputs = await self.checks.run_inputs(run["id"])
                facts, units = caller_inputs(inputs)
                try:
                    persisted, _ = await self.checks.evaluate(
                        revision_id=target_revision_id,
                        objects=target_objects,
                        jurisdiction_code=inputs["jurisdiction_code"],
                        reference_date=inputs["reference_date"],
                        land_parcel=inputs["land_parcel"] or {},
                        facts=facts,
                        fact_units=units,
                        rule_version_ids=rule_ids,
                    )
                except CadCheckInputError as exc:
                    raise DiffInputError(f"cannot re-run checks: {exc}") from exc
                new_results = await self.checks.all_results(target_revision_id, persisted.run["id"])
                target_results += new_results
                rerun_results.append((run, persisted.run, new_results))
                pairs.append(
                    {
                        "base_run_id": run["id"],
                        "target_run_id": persisted.run["id"],
                        "target_run_created": persisted.created,
                    }
                )

        attach_impact(changes, base_results, target_results)
        trans: list[dict[str, Any]] = []
        for base_run, target_run, new_results in rerun_results:
            trans += transitions(
                results_by_run[base_run["id"]],
                new_results,
                changes,
                base_run_id=base_run["id"],
                target_run_id=target_run["id"],
            )

        summary = summarize(changes, len(base_objects), len(target_objects))
        changed = summary["added"] + summary["removed"] + summary["modified"]
        stale_results = sorted({ref["result_id"] for c in changes for ref in c["impact"]["base"]})
        stale = {
            "base_check_run_ids": [r["id"] for r in base_runs],
            "stale_result_ids": stale_results,
            "carried_over": False,
            "target_check_run_count": len(target_runs)
            + sum(1 for p in pairs if p["target_run_created"]),
            "target_unchecked": not (target_runs or pairs),
            "reason": (
                "check runs and results are bound to the base revision; they are not carried "
                "over to the target revision. Run the checks on the target revision "
                "(or rerun_checks) to get its verdicts."
            ),
            "has_changes": changed > 0,
        }
        summary["transition_counts"] = {
            "changed": sum(1 for t in trans if t["changed"]),
            "regressions": sum(1 for t in trans if t["regression"]),
            "improvements": sum(1 for t in trans if t["improvement"]),
        }
        summary["rerun"] = pairs
        relation = (
            "lineage"
            if (base["project_id"], base["source_key"])
            == (target["project_id"], target["source_key"])
            else "paired"
        )
        await self.session.execute(
            text(
                """
                INSERT INTO cad_revision_diff(id, base_revision_id, target_revision_id, relation,
                    jurisdiction_code, tolerances_json, tolerances_digest, rerun_checks,
                    differ_version, base_object_count, target_object_count, added_count,
                    removed_count, modified_count, unchanged_count, summary_json,
                    transitions_json, stale_json)
                VALUES (CAST(:id AS uuid), CAST(:base AS uuid), CAST(:target AS uuid), :rel,
                    :jur, CAST(:tol AS jsonb), :tdigest, :rerun, :ver, :bc, :tc, :ac, :rc,
                    :mc, :uc, CAST(:summary AS jsonb), CAST(:trans AS jsonb),
                    CAST(:stale AS jsonb))
                """
            ),
            {
                "id": diff_id,
                "base": base_revision_id,
                "target": target_revision_id,
                "rel": relation,
                "jur": base["jurisdiction_code"],
                "tol": json.dumps(tolerances.as_dict()),
                "tdigest": tol_digest,
                "rerun": rerun_checks,
                "ver": DIFFER_VERSION,
                "bc": summary["base_object_count"],
                "tc": summary["target_object_count"],
                "ac": summary["added"],
                "rc": summary["removed"],
                "mc": summary["modified"],
                "uc": summary["unchanged"],
                "summary": json.dumps(summary, ensure_ascii=False),
                "trans": json.dumps(trans, ensure_ascii=False),
                "stale": json.dumps(stale, ensure_ascii=False),
            },
        )
        if changes:
            await self.session.execute(
                text(
                    """
                    INSERT INTO cad_object_change(id, diff_id, change_type, kind,
                        base_object_id, target_object_id, match_method, match_score, label,
                        base_handles, target_handles, deltas_json, impact_json)
                    VALUES (CAST(:id AS uuid), CAST(:diff AS uuid), :ct, :kind,
                        CAST(:bo AS uuid), CAST(:to AS uuid), :method, :score, :label,
                        :bh, :th, CAST(:deltas AS jsonb), CAST(:impact AS jsonb))
                    """
                ),
                [
                    {
                        "id": c["id"],
                        "diff": diff_id,
                        "ct": c["change_type"],
                        "kind": c["kind"],
                        "bo": c["base_object_id"],
                        "to": c["target_object_id"],
                        "method": c["match_method"],
                        "score": c["match_score"],
                        "label": c["label"],
                        "bh": c["base_handles"],
                        "th": c["target_handles"],
                        "deltas": json.dumps(c["deltas"], ensure_ascii=False),
                        "impact": json.dumps(c["impact"], ensure_ascii=False),
                    }
                    for c in changes
                ],
            )
        payload = {
            "cad_revision_diff_id": diff_id,
            "base_revision_id": base_revision_id,
            "target_revision_id": target_revision_id,
            "relation": relation,
            "jurisdiction_code": base["jurisdiction_code"],
            "rerun_checks": rerun_checks,
            "added": summary["added"],
            "removed": summary["removed"],
            "modified": summary["modified"],
            "unchanged": summary["unchanged"],
            "regressions": summary["transition_counts"]["regressions"],
        }
        event_id: Any = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO domain_event(aggregate_type, aggregate_id, event_type,
                                             payload_json)
                    VALUES ('cad_revision_diff', :agg, 'CadRevisionDiffed',
                            CAST(:payload AS jsonb))
                    RETURNING event_id
                    """
                ),
                {"agg": diff_id, "payload": json.dumps(payload)},
            )
        ).scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (:event_id, 'cad.revision-diffed', CAST(:payload AS jsonb))
                """
            ),
            {"event_id": event_id, "payload": json.dumps(payload)},
        )
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.revision.diff', 'cad_revision_diff', :id, CAST(:after AS jsonb))
                """
            ),
            {"id": diff_id, "after": json.dumps(payload, sort_keys=True)},
        )
        created = await self.get_diff(diff_id)
        assert created is not None  # noqa: S101 - inserted in this transaction
        return PersistedDiff(created, True)

    async def list_changes(
        self,
        diff_id: str,
        *,
        change_type: str | None,
        kind: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        fp = filter_fingerprint(d=diff_id, t=change_type, k=kind)
        after = decode_cursor(cursor, fp) if cursor else None
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT c.id::text AS id, c.diff_id::text AS diff_id, c.change_type, c.kind,
                           c.base_object_id::text AS base_object_id,
                           c.target_object_id::text AS target_object_id, c.match_method,
                           c.match_score, c.label, c.base_handles, c.target_handles,
                           c.deltas_json AS deltas, c.impact_json AS impact
                      FROM cad_object_change c
                      JOIN cad_revision_diff d ON d.id = c.diff_id
                     WHERE c.diff_id = CAST(:diff AS uuid)
                       AND (CAST(:t AS text) IS NULL OR c.change_type = :t)
                       AND (CAST(:k AS text) IS NULL OR c.kind = :k)
                       AND (CAST(:after AS uuid) IS NULL OR c.id > CAST(:after AS uuid))
                     ORDER BY c.id LIMIT :lim
                    """
                ),
                {"diff": diff_id, "t": change_type, "k": kind, "after": after, "lim": limit + 1},
            )
        ).all()
        items = [dict(r._mapping) for r in rows[:limit]]
        nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
        return items, nxt
