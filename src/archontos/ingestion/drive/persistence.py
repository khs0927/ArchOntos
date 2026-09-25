"""Transactional, metadata-only inventory for Google Drive.

The caller obtains a startPageToken before listing the baseline. It commits
each listing page and then replays changes from that original token. No method
in this module creates a content artifact, source document, or source version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from sqlalchemy import text


class DriveCursorConflict(RuntimeError):
    """Another worker committed a different page from this checkpoint."""


class DriveReceiptMismatch(DriveCursorConflict):
    """A retried page token contained different observations or a next token."""


@dataclass(frozen=True)
class DriveItemSnapshot:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    trashed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    modified_time: str | None = None
    checksum: str | None = None


@dataclass(frozen=True)
class DriveChange:
    file_id: str | None
    removed: bool
    item: DriveItemSnapshot | None = None
    change_type: str = "file"
    drive_id: str | None = None


@dataclass(frozen=True)
class DriveCheckpoint:
    corpus_id: UUID
    scan_id: UUID
    start_token: str
    baseline_complete: bool
    page_token: str | None
    change_token: str


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(payload: Any) -> str:
    return sha256(_json(payload).encode("utf-8")).hexdigest()


def _item_payload(item: DriveItemSnapshot) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "mime_type": item.mime_type,
        "parents": list(item.parents),
        "trashed": item.trashed,
        "metadata": item.metadata,
        "modified_time": item.modified_time,
        "checksum": item.checksum,
    }


def _modified_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Drive modified time must include a timezone")
    return result


def _checkpoint(row: Any) -> DriveCheckpoint:
    return DriveCheckpoint(
        corpus_id=row["id"],
        scan_id=row["baseline_scan_id"],
        start_token=row["start_token"],
        baseline_complete=row["baseline_complete"],
        page_token=row["baseline_page_token"],
        change_token=row["change_token"],
    )


class DriveInventoryRepository:
    """A session factory is a callable producing a SQLAlchemy AsyncSession.

    One page is one database transaction. Repeated identical receipts are
    successful no-ops; the cursor predicates reject stale or divergent pages.
    """

    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    async def _corpus(self, session: Any, account_namespace: str, corpus_key: str,
                      *, lock: bool = False) -> Any:
        locking = "FOR UPDATE OF c" if lock else ""
        result = await session.execute(text(f"""
            SELECT c.*, a.id AS account_id
              FROM drive_corpus c JOIN drive_provider_account a ON a.id = c.account_id
             WHERE a.account_namespace = :account_namespace AND c.corpus_key = :corpus_key
             {locking}
        """), {"account_namespace": account_namespace, "corpus_key": corpus_key})
        row = result.mappings().first()
        if row is None:
            raise KeyError(f"Drive corpus has not been started: {account_namespace}/{corpus_key}")
        return row

    async def begin_scan(self, account_namespace: str, corpus_key: str,
                         start_token: str) -> DriveCheckpoint:
        if not account_namespace or not corpus_key or not start_token:
            raise ValueError("account namespace, corpus key, and start token are required")
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    INSERT INTO drive_provider_account(id, account_namespace)
                    VALUES (:id, :account_namespace)
                    ON CONFLICT (account_namespace) DO NOTHING
                """), {"id": uuid4(), "account_namespace": account_namespace})
                account = await session.execute(text("""
                    SELECT id FROM drive_provider_account
                     WHERE account_namespace = :account_namespace
                """), {"account_namespace": account_namespace})
                account_id = account.scalar_one()
                await session.execute(text("""
                    INSERT INTO drive_corpus(id, account_id, corpus_key, start_token,
                                             baseline_scan_id, change_token)
                    VALUES (:id, :account_id, :corpus_key, :start_token,
                            :scan_id, :start_token)
                    ON CONFLICT (account_id, corpus_key) DO NOTHING
                """), {"id": uuid4(), "account_id": account_id, "corpus_key": corpus_key,
                       "start_token": start_token, "scan_id": uuid4()})
                row = await self._corpus(session, account_namespace, corpus_key, lock=True)
                # In particular, a retry must never replace an in-progress scan.
                return _checkpoint(row)

    async def get_checkpoint(self, account_namespace: str,
                             corpus_key: str) -> DriveCheckpoint | None:
        async with self.session_factory() as session:
            result = await session.execute(text("""
                SELECT c.* FROM drive_corpus c
                JOIN drive_provider_account a ON a.id = c.account_id
                WHERE a.account_namespace = :account_namespace AND c.corpus_key = :corpus_key
            """), {"account_namespace": account_namespace, "corpus_key": corpus_key})
            row = result.mappings().first()
            return _checkpoint(row) if row is not None else None

    async def _receipt(self, session: Any, kind: str, corpus_id: UUID,
                       scan_or_token: UUID | str, input_token: str,
                       digest: str | None = None) -> Any:
        if kind == "baseline":
            query = """
                SELECT * FROM drive_baseline_page_receipt
                WHERE corpus_id = :corpus_id AND scan_id = :scan_id
                  AND input_page_token = :input_token
            """
            params = {"corpus_id": corpus_id, "scan_id": scan_or_token,
                      "input_token": input_token}
        else:
            query = """
                SELECT * FROM drive_change_page_receipt
                WHERE corpus_id = :corpus_id AND input_change_token = :input_token
                  AND page_fingerprint = :digest
            """
            params = {"corpus_id": corpus_id, "input_token": input_token,
                      "digest": digest}
        result = await session.execute(text(query), params)
        return result.mappings().first()

    async def _upsert_item(self, session: Any, account_id: UUID, file_id: str,
                           item: DriveItemSnapshot | None) -> UUID:
        if not file_id:
            raise ValueError("file ID is required for a file change")
        if item is not None and item.id != file_id:
            raise ValueError("file ID and snapshot ID differ")
        if item is None:
            result = await session.execute(text("""
                INSERT INTO drive_item(id, account_id, provider_file_id)
                VALUES (:id, :account_id, :file_id)
                ON CONFLICT (account_id, provider_file_id)
                DO UPDATE SET last_observed_at = now()
                RETURNING id
            """), {"id": uuid4(), "account_id": account_id, "file_id": file_id})
        else:
            result = await session.execute(text("""
                INSERT INTO drive_item(id, account_id, provider_file_id, drive_id,
                                       name, mime_type, modified_time, checksum, metadata_json)
                VALUES (:id, :account_id, :file_id, :drive_id,
                        :name, :mime_type, :modified_time, :checksum,
                        CAST(:metadata AS jsonb))
                ON CONFLICT (account_id, provider_file_id) DO UPDATE SET
                    drive_id = EXCLUDED.drive_id, name = EXCLUDED.name,
                    mime_type = EXCLUDED.mime_type,
                    modified_time = EXCLUDED.modified_time,
                    checksum = EXCLUDED.checksum,
                    metadata_json = EXCLUDED.metadata_json,
                    last_observed_at = now()
                RETURNING id
            """), {"id": uuid4(), "account_id": account_id, "file_id": file_id,
                   "drive_id": item.metadata.get("driveId"), "name": item.name,
                   "mime_type": item.mime_type,
                   "modified_time": _modified_datetime(item.modified_time),
                   "checksum": item.checksum, "metadata": _json(item.metadata)})
        return result.scalar_one()

    async def _emit(self, session: Any, corpus_id: UUID, event_type: str,
                    payload: dict[str, Any]) -> None:
        event_id = uuid4()
        data = {"event_id": event_id, "corpus_id": str(corpus_id),
                "event_type": event_type, "payload": _json(payload)}
        await session.execute(text("""
            INSERT INTO domain_event(event_id, aggregate_type, aggregate_id,
                                     event_type, payload_json)
            VALUES (:event_id, 'drive_corpus', :corpus_id,
                    :event_type, CAST(:payload AS jsonb))
        """), data)
        await session.execute(text("""
            INSERT INTO outbox_message(event_id, topic, payload_json)
            VALUES (:event_id, 'drive.inventory', CAST(:payload AS jsonb))
        """), data)

    async def _membership(self, session: Any, corpus_id: UUID, item_id: UUID,
                          file_id: str, *, reason: str | None,
                          scan_id: UUID | None = None) -> None:
        previous = await session.execute(text("""
            SELECT status, reason FROM drive_corpus_membership
            WHERE corpus_id = :corpus_id AND item_id = :item_id FOR UPDATE
        """), {"corpus_id": corpus_id, "item_id": item_id})
        old = previous.mappings().first()
        status = "BLOCKED" if reason else "ACTIVE"
        await session.execute(text("""
            INSERT INTO drive_corpus_membership
                (corpus_id, item_id, status, reason, last_seen_scan_id)
            VALUES (:corpus_id, :item_id, :status, :reason, :scan_id)
            ON CONFLICT (corpus_id, item_id) DO UPDATE SET
                status = EXCLUDED.status, reason = EXCLUDED.reason,
                last_seen_scan_id = COALESCE(EXCLUDED.last_seen_scan_id,
                                             drive_corpus_membership.last_seen_scan_id),
                last_observed_at = now()
        """), {"corpus_id": corpus_id, "item_id": item_id, "status": status,
               "reason": reason, "scan_id": scan_id})
        if reason and (old is None or old["status"] != "BLOCKED" or old["reason"] != reason):
            await self._emit(session, corpus_id, "DRIVE_MEMBERSHIP_BLOCKED",
                             {"file_id": file_id, "reason": reason})
        elif not reason and old is not None and old["status"] == "BLOCKED":
            await self._emit(session, corpus_id, "DRIVE_MEMBERSHIP_RESTORED",
                             {"file_id": file_id})

    async def _observe(self, session: Any, item_id: UUID, ordinal: int,
                       *, baseline_receipt_id: UUID | None = None,
                       change_receipt_id: UUID | None = None,
                       state: str, metadata: dict[str, Any]) -> None:
        await session.execute(text("""
            INSERT INTO drive_metadata_observation
                (id, item_id, item_ordinal, baseline_receipt_id,
                 change_receipt_id, state, metadata_json)
            VALUES (:id, :item_id, :ordinal, :baseline_receipt_id,
                    :change_receipt_id, :state, CAST(:metadata AS jsonb))
        """), {"id": uuid4(), "item_id": item_id, "ordinal": ordinal,
               "baseline_receipt_id": baseline_receipt_id,
               "change_receipt_id": change_receipt_id, "state": state,
               "metadata": _json(metadata)})

    async def _block_missing(self, session: Any, corpus_id: UUID,
                             scan_id: UUID) -> None:
        # A missing listing entry is only a corpus-specific access uncertainty.
        # Replay changes from the pre-listing token before inferring deletion.
        result = await session.execute(text("""
            SELECT m.item_id, i.provider_file_id FROM drive_corpus_membership m
            JOIN drive_item i ON i.id = m.item_id
            WHERE m.corpus_id = :corpus_id AND m.status = 'ACTIVE'
              AND m.last_seen_scan_id IS DISTINCT FROM :scan_id
            FOR UPDATE OF m
        """), {"corpus_id": corpus_id, "scan_id": scan_id})
        for row in result.mappings().all():
            await self._membership(session, corpus_id, row["item_id"],
                                   row["provider_file_id"],
                                   reason="NOT_OBSERVED_BASELINE")

    async def commit_baseline_page(self, account_namespace: str, corpus_key: str,
                                   expected_page_token: str | None,
                                   next_page_token: str | None,
                                   items: Iterable[DriveItemSnapshot],
                                   page_fingerprint: str | None = None,
                                   is_last: bool = False) -> DriveCheckpoint:
        items = tuple(items)
        if is_last != (next_page_token is None):
            raise ValueError("only the last baseline page may lack a next page token")
        digest = _fingerprint({"items": [_item_payload(i) for i in items],
                               "next": next_page_token, "last": is_last,
                               "provider_page_fingerprint": page_fingerprint})
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._corpus(session, account_namespace, corpus_key, lock=True)
                receipt = await self._receipt(session, "baseline", row["id"],
                                              row["baseline_scan_id"],
                                              expected_page_token or "")
                if receipt is not None:
                    if (receipt["page_fingerprint"] != digest or
                            receipt["output_page_token"] != next_page_token or
                            receipt["is_last"] != is_last):
                        raise DriveReceiptMismatch(
                            "baseline page token was committed with different data"
                        )
                    return _checkpoint(row)
                if row["baseline_complete"] or row["baseline_page_token"] != expected_page_token:
                    raise DriveCursorConflict("baseline page cursor changed")
                receipt_id = uuid4()
                await session.execute(text("""
                    INSERT INTO drive_baseline_page_receipt
                        (id, corpus_id, scan_id, input_page_token, output_page_token,
                         page_fingerprint, is_last, item_count)
                    VALUES (:id, :corpus_id, :scan_id, :input_token, :output_token,
                            :digest, :is_last, :item_count)
                """), {"id": receipt_id, "corpus_id": row["id"],
                       "scan_id": row["baseline_scan_id"],
                       "input_token": expected_page_token or "",
                       "output_token": next_page_token, "digest": digest,
                       "is_last": is_last, "item_count": len(items)})
                for ordinal, item in enumerate(items):
                    item_id = await self._upsert_item(session, row["account_id"], item.id, item)
                    await self._observe(session, item_id, ordinal,
                                        baseline_receipt_id=receipt_id,
                                        state="METADATA_ONLY", metadata=_item_payload(item))
                    await self._membership(session, row["id"], item_id, item.id,
                                           reason="TRASHED" if item.trashed else None,
                                           scan_id=row["baseline_scan_id"])
                updated = await session.execute(text("""
                    UPDATE drive_corpus SET baseline_page_token = :next_token,
                        baseline_last_page_seen = :is_last,
                        baseline_complete = :is_last, updated_at = now()
                    WHERE id = :corpus_id AND baseline_complete = false
                      AND baseline_page_token IS NOT DISTINCT FROM :expected
                    RETURNING *
                """), {"corpus_id": row["id"], "expected": expected_page_token,
                       "next_token": next_page_token, "is_last": is_last})
                new_row = updated.mappings().first()
                if new_row is None:
                    raise DriveCursorConflict("baseline compare-and-swap failed")
                if is_last:
                    await self._block_missing(session, row["id"], row["baseline_scan_id"])
                return _checkpoint(new_row)

    async def finish_baseline(self, account_namespace: str, corpus_key: str,
                              expected_page_token: str | None = None) -> DriveCheckpoint:
        """Idempotent finalization after the terminal page was committed.

        Terminal commit already completes and reconciles the baseline atomically.
        This method deliberately cannot finalize an incomplete page chain.
        """
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._corpus(session, account_namespace, corpus_key, lock=True)
                if (
                    not row["baseline_complete"]
                    or row["baseline_page_token"] != expected_page_token
                ):
                    raise DriveCursorConflict("last baseline page has not committed")
                return _checkpoint(row)

    async def _drive_change(self, session: Any, row: Any, receipt_id: UUID,
                            ordinal: int, change: DriveChange) -> None:
        if not change.drive_id:
            raise ValueError("drive change requires drive_id")
        await session.execute(text("""
            INSERT INTO drive_access_observation
                (id, change_receipt_id, change_ordinal, drive_id, removed)
            VALUES (:id, :receipt_id, :ordinal, :drive_id, :removed)
        """), {"id": uuid4(), "receipt_id": receipt_id, "ordinal": ordinal,
               "drive_id": change.drive_id, "removed": change.removed})
        if change.removed:
            matches = await session.execute(text("""
                SELECT m.corpus_id, m.item_id, i.provider_file_id
                FROM drive_corpus_membership m JOIN drive_item i ON i.id = m.item_id
                WHERE i.account_id = :account_id
                  AND (i.drive_id = :drive_id OR
                       (m.corpus_id = :corpus_id AND :corpus_key = :shared_corpus_key))
                  AND m.status = 'ACTIVE'
                FOR UPDATE OF m
            """), {"account_id": row["account_id"], "drive_id": change.drive_id,
                   "corpus_id": row["id"], "corpus_key": row["corpus_key"],
                   "shared_corpus_key": f"drive:{change.drive_id}"})
            for member in matches.mappings().all():
                await self._membership(session, member["corpus_id"], member["item_id"],
                                       member["provider_file_id"],
                                       reason="SHARED_DRIVE_ACCESS_REMOVED")
        await self._emit(session, row["id"], "DRIVE_ACCESS_CHANGED",
                         {"drive_id": change.drive_id, "removed": change.removed})

    async def commit_change_page(self, account_namespace: str, corpus_key: str,
                                 expected_change_token: str, next_change_token: str,
                                 changes: Iterable[DriveChange],
                                 page_fingerprint: str | None = None,
                                 is_last: bool = False) -> DriveCheckpoint:
        if not expected_change_token or not next_change_token:
            raise ValueError("both change tokens are required")
        changes = tuple(changes)
        if next_change_token == expected_change_token and (changes or not is_last):
            raise ValueError("only an empty terminal poll may keep the change token")
        digest = _fingerprint({
            "changes": [{"file_id": c.file_id, "removed": c.removed,
                         "item": _item_payload(c.item) if c.item else None,
                         "change_type": c.change_type, "drive_id": c.drive_id}
                        for c in changes],
            "next": next_change_token, "last": is_last,
            "provider_page_fingerprint": page_fingerprint,
        })
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._corpus(session, account_namespace, corpus_key, lock=True)
                receipt = await self._receipt(session, "changes", row["id"], "",
                                              expected_change_token, digest)
                if receipt is not None:
                    if (receipt["page_fingerprint"] != digest or
                            receipt["output_change_token"] != next_change_token or
                            receipt["is_last"] != is_last):
                        raise DriveReceiptMismatch(
                            "change page token was committed with different data"
                        )
                    return _checkpoint(row)
                if not row["baseline_complete"] or row["change_token"] != expected_change_token:
                    raise DriveCursorConflict("change cursor changed or baseline is incomplete")
                receipt_id = uuid4()
                await session.execute(text("""
                    INSERT INTO drive_change_page_receipt
                        (id, corpus_id, input_change_token, output_change_token,
                         page_fingerprint, is_last, change_count)
                    VALUES (:id, :corpus_id, :input_token, :output_token,
                            :digest, :is_last, :change_count)
                """), {"id": receipt_id, "corpus_id": row["id"],
                       "input_token": expected_change_token,
                       "output_token": next_change_token, "digest": digest,
                       "is_last": is_last, "change_count": len(changes)})
                for ordinal, change in enumerate(changes):
                    if change.change_type == "drive":
                        await self._drive_change(session, row, receipt_id, ordinal, change)
                        continue
                    if change.change_type != "file":
                        raise ValueError(f"unsupported change type: {change.change_type}")
                    file_id = change.file_id or (change.item.id if change.item else None)
                    if file_id is None:
                        raise ValueError("file change requires file_id or item")
                    item_id = await self._upsert_item(session, row["account_id"],
                                                      file_id, change.item)
                    blocked = "REMOVED_OR_INACCESSIBLE" if change.removed else (
                        "TRASHED" if change.item and change.item.trashed else None)
                    observation = {"file_id": file_id, "removed": change.removed,
                                   "item": _item_payload(change.item) if change.item else None}
                    await self._observe(session, item_id, ordinal,
                                        change_receipt_id=receipt_id,
                                        state="REMOVED" if change.removed else "METADATA_ONLY",
                                        metadata=observation)
                    await self._membership(session, row["id"], item_id, file_id,
                                           reason=blocked)
                updated = await session.execute(text("""
                    UPDATE drive_corpus SET change_token = :next_token, updated_at = now()
                    WHERE id = :corpus_id AND baseline_complete = true
                      AND change_token = :expected
                    RETURNING *
                """), {"corpus_id": row["id"], "next_token": next_change_token,
                       "expected": expected_change_token})
                new_row = updated.mappings().first()
                if new_row is None:
                    raise DriveCursorConflict("change cursor compare-and-swap failed")
                return _checkpoint(new_row)
