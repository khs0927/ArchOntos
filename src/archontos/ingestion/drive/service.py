"""Resumable metadata-only Drive inventory and change replay orchestration.

Every page is acknowledged by the repository in its own transaction. The
provider's page token is never advanced before its observations are committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from archontos.ingestion.drive.persistence import DriveChange, DriveItemSnapshot


def _snapshot(raw: dict[str, Any]) -> DriveItemSnapshot:
    file_id = raw.get("id")
    name = raw.get("name")
    if not isinstance(file_id, str) or not file_id:
        raise ValueError("Drive file has no id")
    if not isinstance(name, str):
        raise ValueError("Drive file has no name")
    parents = raw.get("parents", [])
    if not isinstance(parents, list) or not all(isinstance(x, str) for x in parents):
        raise ValueError("Drive file parents must be a list of ids")
    return DriveItemSnapshot(
        id=file_id,
        name=name,
        mime_type=raw.get("mimeType", "application/octet-stream"),
        parents=tuple(parents),
        trashed=raw.get("trashed", False),
        metadata=raw,
        modified_time=raw.get("modifiedTime"),
        checksum=raw.get("md5Checksum"),
    )


@dataclass(frozen=True)
class CorpusResult:
    corpus_key: str
    baseline_pages: int
    change_pages: int
    observed_files: int
    removed_events: int


class DriveInventoryService:
    def __init__(
        self,
        *,
        adapter: Any,
        repository: Any,
        account_namespace: str,
        max_pages_per_corpus: int = 100_000,
    ) -> None:
        if not account_namespace:
            raise ValueError("account_namespace is required")
        self.adapter = adapter
        self.repository = repository
        self.account_namespace = account_namespace
        self.max_pages_per_corpus = max_pages_per_corpus

    async def _checkpoint(self, corpus_key: str, drive_id: str | None) -> Any:
        checkpoint = await self.repository.get_checkpoint(self.account_namespace, corpus_key)
        if checkpoint is None:
            token = await self.adapter.start_page_token(
                "drive" if drive_id else "user", drive_id=drive_id
            )
            checkpoint = await self.repository.begin_scan(self.account_namespace, corpus_key, token)
        return checkpoint

    async def _sync_corpus(self, corpus_key: str, drive_id: str | None) -> CorpusResult:
        checkpoint = await self._checkpoint(corpus_key, drive_id)
        baseline_pages = change_pages = observed_files = removed_events = 0
        seen_tokens: set[str] = set()

        while not checkpoint.baseline_complete:
            if baseline_pages >= self.max_pages_per_corpus:
                raise RuntimeError("Drive baseline page budget exceeded")
            page_token = checkpoint.page_token
            marker = page_token if page_token is not None else "<first>"
            if marker in seen_tokens:
                raise RuntimeError("Drive baseline pagination loop")
            seen_tokens.add(marker)
            page = await self.adapter.list_files_page(
                "drive" if drive_id else "user", drive_id=drive_id, page_token=page_token
            )
            if page.get("incompleteSearch"):
                raise RuntimeError("Drive returned incompleteSearch; baseline not committed")
            items = [_snapshot(raw) for raw in page["files"]]
            next_token = page.get("nextPageToken")
            checkpoint = await self.repository.commit_baseline_page(
                self.account_namespace,
                corpus_key,
                page_token,
                next_token,
                items,
                is_last=not next_token,
            )
            baseline_pages += 1
            observed_files += len(items)

        seen_tokens.clear()
        while True:
            if change_pages >= self.max_pages_per_corpus:
                raise RuntimeError("Drive change page budget exceeded")
            token = checkpoint.change_token
            if token in seen_tokens:
                raise RuntimeError("Drive change pagination loop")
            seen_tokens.add(token)
            page = await self.adapter.list_changes_page(token, drive_id=drive_id)
            changes: list[DriveChange] = []
            for raw in page["changes"]:
                if raw.get("changeType") == "drive" or (
                    not raw.get("fileId") and raw.get("driveId")
                ):
                    changed_drive = raw.get("driveId")
                    if not isinstance(changed_drive, str) or not changed_drive:
                        raise ValueError("Drive-level change has no driveId")
                    changes.append(
                        DriveChange(
                            file_id=None,
                            removed=bool(raw.get("removed")),
                            item=None,
                            change_type="drive",
                            drive_id=changed_drive,
                        )
                    )
                    continue
                file_id = raw.get("fileId")
                if not isinstance(file_id, str) or not file_id:
                    raise ValueError("Drive change has no fileId")
                removed = bool(raw.get("removed", False))
                if not removed and not isinstance(raw.get("file"), dict):
                    raise ValueError("Drive change has no file metadata")
                item = None if removed else _snapshot(raw["file"])
                if item is not None and item.id != file_id:
                    raise ValueError("Drive change fileId differs from file.id")
                changes.append(
                    DriveChange(
                        file_id=file_id, removed=removed, item=item, drive_id=raw.get("driveId")
                    )
                )
            next_token = page.get("nextPageToken")
            terminal_token = page.get("newStartPageToken")
            if not next_token and not terminal_token:
                raise ValueError("Drive terminal change page has no newStartPageToken")
            checkpoint = await self.repository.commit_change_page(
                self.account_namespace,
                corpus_key,
                token,
                next_token or terminal_token,
                changes,
                is_last=not next_token,
            )
            change_pages += 1
            observed_files += sum(change.item is not None for change in changes)
            removed_events += sum(change.removed for change in changes)
            if not next_token:
                break

        return CorpusResult(
            corpus_key, baseline_pages, change_pages, observed_files, removed_events
        )

    async def sync_all(self) -> list[CorpusResult]:
        # Capture the user feed before discovering shared drives, then capture
        # each drive's feed before scanning any baseline page.
        await self._checkpoint("user", None)
        drives = await self.adapter.list_drives()
        drive_ids = sorted({drive["id"] for drive in drives})
        for drive_id in drive_ids:
            await self._checkpoint(f"drive:{drive_id}", drive_id)
        result = [await self._sync_corpus("user", None)]
        for drive_id in drive_ids:
            result.append(await self._sync_corpus(f"drive:{drive_id}", drive_id))
        return result
