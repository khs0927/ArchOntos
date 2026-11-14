"""Append-only issue notes (``cad_issue_note``, migrations 019 + 021).

Notes are typed by an actor (``kind = 'note'``, author = request actor) or imported from a BCF
file (``bcf_comment`` / ``bcf_status``, idempotent by the external guid). The issue is always
read through ``cad_issue`` first, so the jurisdiction RLS of the issue also governs its notes.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.cad.issue_persistence import CadIssueRepository
from archontos.cad.persistence import InvalidCadCursor, filter_fingerprint

NOTE_BODY_MAX = 4000
_SELECT = """
    SELECT n.id::text AS id, n.issue_id::text AS issue_id, n.body, n.kind, n.external_guid,
           n.external_author, n.external_at, n.created_by, n.created_at
      FROM cad_issue_note n
"""
_RETURNING = """
    RETURNING id::text AS id, issue_id::text AS issue_id, body, kind, external_guid,
              external_author, external_at, created_by, created_at
"""


_INSERT_NOTE = """
INSERT INTO cad_issue_note(issue_id, body, created_at)
VALUES (CAST(:issue AS uuid), :body, clock_timestamp())
"""
_INSERT_IMPORTED = """
INSERT INTO cad_issue_note(issue_id, body, kind, external_guid,
    external_author, external_at, created_at)
VALUES (CAST(:issue AS uuid), :body, :kind, :guid, :author, :at,
        clock_timestamp())
ON CONFLICT (issue_id, external_guid) WHERE external_guid IS NOT NULL
DO NOTHING
"""


class IssueNotFound(LookupError):
    pass


def _note_row(row: Any) -> dict[str, Any]:
    out = dict(row._mapping)
    for key in ("created_at", "external_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def _encode_cursor(created_at: str, note_id: str, fingerprint: str) -> str:
    raw = json.dumps({"t": created_at, "i": note_id, "f": fingerprint}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        at = datetime.fromisoformat(str(data["t"]))
        nid = str(UUID(str(data["i"])))
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCadCursor("malformed cursor") from exc
    if data.get("f") != fingerprint:
        raise InvalidCadCursor("cursor belongs to a different listing")
    return at, nid


class CadIssueNoteRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def require_issue(self, issue_id: str) -> None:
        if await CadIssueRepository(self.session).get_issue(issue_id) is None:
            raise IssueNotFound("issue not found")

    async def add(self, issue_id: str, body: str) -> dict[str, Any]:
        """Append a note; the author is ``app.actor`` (set from the request identity)."""
        await self.require_issue(issue_id)
        row = (
            await self.session.execute(
                text(_INSERT_NOTE + _RETURNING),
                {"issue": issue_id, "body": body},
            )
        ).one()
        note = _note_row(row)
        await self.session.execute(
            text(
                """
                INSERT INTO audit_log(action, target_table, target_id, after_json)
                VALUES ('cad.issue.note', 'cad_issue_note', :id, CAST(:after AS jsonb))
                """
            ),
            {"id": note["id"], "after": json.dumps({"issue_id": issue_id, "kind": "note"})},
        )
        return note

    async def add_imported(
        self,
        issue_id: str,
        *,
        kind: str,
        body: str,
        external_guid: str,
        external_author: str | None,
        external_at: datetime | None,
    ) -> dict[str, Any] | None:
        """Insert an imported note; ``None`` when this external guid is already stored."""
        row = (
            await self.session.execute(
                text(_INSERT_IMPORTED + _RETURNING),
                {
                    "issue": issue_id,
                    "body": body[:NOTE_BODY_MAX],
                    "kind": kind,
                    "guid": external_guid,
                    "author": external_author[:200] if external_author else None,
                    "at": external_at,
                },
            )
        ).first()
        return _note_row(row) if row else None

    async def list(
        self, issue_id: str, *, limit: int, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        await self.require_issue(issue_id)
        fp = filter_fingerprint(issue=issue_id)
        after_at, after_id = _decode_cursor(cursor, fp) if cursor else (None, None)
        rows = (
            await self.session.execute(
                text(
                    _SELECT
                    + """
                     WHERE n.issue_id = CAST(:issue AS uuid)
                       AND (CAST(:at AS timestamptz) IS NULL
                            OR (n.created_at, n.id) > (CAST(:at AS timestamptz),
                                                       CAST(:nid AS uuid)))
                     ORDER BY n.created_at, n.id LIMIT :lim
                    """
                ),
                {"issue": issue_id, "at": after_at, "nid": after_id, "lim": limit + 1},
            )
        ).all()
        items = [_note_row(r) for r in rows[:limit]]
        nxt = (
            _encode_cursor(items[-1]["created_at"], items[-1]["id"], fp)
            if len(rows) > limit
            else None
        )
        return items, nxt
