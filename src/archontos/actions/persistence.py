from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.actions.gate import ApprovalDenied, assert_can_execute
from archontos.actions.service import ProposedAction
from archontos.identity import bind_actor


@dataclass(slots=True)
class StoredAction:
    id: str
    action_type: str
    target_refs: list[str]
    input_payload: dict[str, Any]
    proposed_output: dict[str, Any]
    status: str
    requires_approval: bool
    runs: list[dict[str, Any]] = field(default_factory=list)
    created_by: str = "system"

    def as_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action_type": self.action_type,
            "target_refs": self.target_refs,
            "input_payload": self.input_payload,
            "proposed_output": self.proposed_output,
            "status": self.status,
            "requires_approval": self.requires_approval,
            "runs": self.runs,
            "created_by": self.created_by,
        }


class MemoryActionStore:
    """Process-local store. Production uses PostgresActionStore."""

    def __init__(self) -> None:
        self._rows: dict[str, StoredAction] = {}

    async def propose(self, proposal: ProposedAction, created_by: str = "system") -> StoredAction:
        row = StoredAction(
            id=str(uuid4()),
            action_type=proposal.action_type,
            target_refs=list(proposal.target_refs),
            input_payload=dict(proposal.input_payload),
            proposed_output=dict(proposal.proposed_output),
            status="proposed",
            requires_approval=proposal.requires_approval,
            created_by=created_by,
        )
        self._rows[row.id] = row
        return row

    async def get(self, action_id: str) -> StoredAction | None:
        return self._rows.get(action_id)

    async def approve(self, action_id: str, actor: str) -> StoredAction:
        row = self._require(action_id)
        if row.status != "proposed":
            raise ApprovalDenied(row.status, row.requires_approval)
        row.status = "approved"
        row.input_payload = {**row.input_payload, "approved_by": actor}
        return row

    async def reject(self, action_id: str, actor: str, reason: str) -> StoredAction:
        row = self._require(action_id)
        if row.status not in {"proposed", "approved"}:
            raise ApprovalDenied(row.status, row.requires_approval)
        row.status = "rejected"
        row.input_payload = {**row.input_payload, "rejected_by": actor, "reject_reason": reason}
        return row

    async def execute(self, action_id: str, actor: str) -> StoredAction:
        row = self._require(action_id)
        assert_can_execute(status=row.status, requires_approval=row.requires_approval)
        row.status = "running"
        result = {
            "format": row.proposed_output.get("format", "json"),
            "status": "generated",
            "actor": actor,
            "target_refs": row.target_refs,
        }
        row.runs.append({"result": result, "error": None})
        row.status = "succeeded"
        row.proposed_output = {**row.proposed_output, "status": "generated"}
        return row

    def _require(self, action_id: str) -> StoredAction:
        row = self._rows.get(action_id)
        if row is None:
            raise KeyError(action_id)
        return row


class PostgresActionStore:
    """Persist proposals and enforce the approval gate in canonical tables."""

    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def propose(self, proposal: ProposedAction, created_by: str = "system") -> StoredAction:
        async with self.session_factory() as session:
            async with session.begin():
                await bind_actor(session, created_by)
                inserted = await session.execute(
                    text(
                        """
                        INSERT INTO action(
                            action_type, target_refs_json, input_json,
                            proposed_output_json, status, requires_approval, created_by
                        )
                        VALUES (
                            :action_type, CAST(:targets AS jsonb), CAST(:input_json AS jsonb),
                            CAST(:output_json AS jsonb), 'proposed', :requires_approval,
                            :created_by
                        )
                        RETURNING id::text
                        """
                    ),
                    {
                        "action_type": proposal.action_type,
                        "targets": json.dumps(proposal.target_refs),
                        "input_json": json.dumps(proposal.input_payload, sort_keys=True),
                        "output_json": json.dumps(proposal.proposed_output, sort_keys=True),
                        "requires_approval": proposal.requires_approval,
                        "created_by": created_by,
                    },
                )
                action_id = inserted.scalar_one()
                await self._audit(
                    session,
                    actor=created_by,
                    action="propose",
                    target_id=action_id,
                    after={"status": "proposed", "action_type": proposal.action_type},
                )
        loaded = await self.get(action_id)
        if loaded is None:
            raise RuntimeError("row vanished after commit")
        return loaded

    async def get(self, action_id: str) -> StoredAction | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text, action_type, target_refs_json, input_json,
                               proposed_output_json, status, requires_approval, created_by
                        FROM action WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {"id": action_id},
                )
            ).first()
            if row is None:
                return None
            runs = (
                await session.execute(
                    text(
                        """
                        SELECT result_json, error_json
                        FROM action_run WHERE action_id = CAST(:id AS uuid)
                        ORDER BY executed_at
                        """
                    ),
                    {"id": action_id},
                )
            ).all()
        return StoredAction(
            id=row.id,
            action_type=row.action_type,
            target_refs=list(row.target_refs_json),
            input_payload=dict(row.input_json),
            proposed_output=dict(row.proposed_output_json),
            status=row.status,
            requires_approval=bool(row.requires_approval),
            created_by=row.created_by,
            runs=[{"result": item.result_json, "error": item.error_json} for item in runs],
        )

    async def approve(self, action_id: str, actor: str) -> StoredAction:
        return await self._transition(action_id, actor, "approved", expect="proposed")

    async def reject(self, action_id: str, actor: str, reason: str) -> StoredAction:
        return await self._transition(
            action_id, actor, "rejected", expect=("proposed", "approved"), reason=reason
        )

    async def execute(self, action_id: str, actor: str) -> StoredAction:
        async with self.session_factory() as session:
            async with session.begin():
                await bind_actor(session, actor)
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT status, requires_approval, proposed_output_json, target_refs_json
                            FROM action WHERE id = CAST(:id AS uuid)
                            FOR UPDATE
                            """
                        ),
                        {"id": action_id},
                    )
                ).first()
                if row is None:
                    raise KeyError(action_id)
                assert_can_execute(status=row.status, requires_approval=bool(row.requires_approval))
                await session.execute(
                    text("UPDATE action SET status = 'running' WHERE id = CAST(:id AS uuid)"),
                    {"id": action_id},
                )
                result = {
                    "format": dict(row.proposed_output_json).get("format", "json"),
                    "status": "generated",
                    "actor": actor,
                    "target_refs": list(row.target_refs_json),
                }
                await session.execute(
                    text(
                        """
                        INSERT INTO action_run(action_id, result_json)
                        VALUES (CAST(:id AS uuid), CAST(:result AS jsonb))
                        """
                    ),
                    {"id": action_id, "result": json.dumps(result, sort_keys=True)},
                )
                await session.execute(
                    text(
                        """
                        UPDATE action
                        SET status = 'succeeded',
                            proposed_output_json = CAST(:output AS jsonb)
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {
                        "id": action_id,
                        "output": json.dumps(
                            {**dict(row.proposed_output_json), "status": "generated"}
                        ),
                    },
                )
                await self._audit(
                    session,
                    actor=actor,
                    action="execute",
                    target_id=action_id,
                    after={"status": "succeeded"},
                )
        loaded = await self.get(action_id)
        if loaded is None:
            raise RuntimeError("row vanished after commit")
        return loaded

    async def _transition(
        self,
        action_id: str,
        actor: str,
        to_status: str,
        expect: str | tuple[str, ...],
        reason: str | None = None,
    ) -> StoredAction:
        allowed = (expect,) if isinstance(expect, str) else expect
        async with self.session_factory() as session:
            async with session.begin():
                await bind_actor(session, actor)
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT status, requires_approval
                            FROM action WHERE id = CAST(:id AS uuid)
                            FOR UPDATE
                            """
                        ),
                        {"id": action_id},
                    )
                ).first()
                if row is None:
                    raise KeyError(action_id)
                if row.status not in allowed:
                    raise ApprovalDenied(row.status, bool(row.requires_approval))
                await session.execute(
                    text("UPDATE action SET status = :status WHERE id = CAST(:id AS uuid)"),
                    {"id": action_id, "status": to_status},
                )
                await self._audit(
                    session,
                    actor=actor,
                    action=to_status,
                    target_id=action_id,
                    after={"status": to_status, "reason": reason},
                )
        loaded = await self.get(action_id)
        if loaded is None:
            raise RuntimeError("row vanished after commit")
        return loaded

    @staticmethod
    async def _audit(session, *, actor: str, action: str, target_id: str, after: dict) -> None:
        await session.execute(
            text(
                """
                INSERT INTO audit_log(actor, action, target_table, target_id, after_json)
                VALUES (:actor, :action, 'action', :target_id, CAST(:after AS jsonb))
                """
            ),
            {
                "actor": actor,
                "action": action,
                "target_id": target_id,
                "after": json.dumps(after, sort_keys=True),
            },
        )


def proposal_dict(proposal: ProposedAction) -> dict[str, Any]:
    return asdict(proposal)


def parse_action_id(value: str) -> str:
    return str(UUID(value))
