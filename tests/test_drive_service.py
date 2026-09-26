from dataclasses import dataclass

import pytest

from archontos.ingestion.drive.service import DriveInventoryService


@dataclass
class Checkpoint:
    page_token: str | None
    change_token: str
    baseline_complete: bool = False


class MemoryRepository:
    def __init__(self):
        self.checkpoints = {}
        self.observed = []
        self.changes = []

    async def get_checkpoint(self, account, corpus):
        return self.checkpoints.get((account, corpus))

    async def begin_scan(self, account, corpus, start_token):
        key = account, corpus
        return self.checkpoints.setdefault(key, Checkpoint(None, start_token))

    async def commit_baseline_page(
        self, account, corpus, expected, next_token, items, fingerprint=None, *, is_last
    ):
        cp = self.checkpoints[(account, corpus)]
        assert cp.page_token == expected
        self.observed.extend((corpus, item.id) for item in items)
        cp.page_token = next_token
        cp.baseline_complete = is_last
        return cp

    async def commit_change_page(
        self, account, corpus, expected, next_token, changes, fingerprint=None, *, is_last
    ):
        cp = self.checkpoints[(account, corpus)]
        assert cp.change_token == expected
        self.changes.extend((corpus, item.file_id, item.removed) for item in changes)
        cp.change_token = next_token
        return cp


class FakeAdapter:
    def __init__(self, fail_once=False):
        self.log = []
        self.fail_once = fail_once

    async def start_page_token(self, corpus, drive_id=None):
        self.log.append(("start", corpus, drive_id))
        return "initial:" + (drive_id or corpus)

    async def list_drives(self):
        self.log.append(("drives",))
        return [{"id": "shared-1"}]

    async def list_files_page(self, corpus, drive_id=None, page_token=None):
        self.log.append(("files", corpus, drive_id, page_token))
        if self.fail_once:
            self.fail_once = False
            raise OSError("temporary network failure")
        if corpus == "drive":
            return {"files": [], "nextPageToken": None, "incompleteSearch": False}
        if page_token is None:
            return {"files": [], "nextPageToken": "next", "incompleteSearch": False}
        return {
            "files": [{"id": "file-1", "name": "A.dwg", "parents": []}],
            "nextPageToken": None,
            "incompleteSearch": False,
        }

    async def list_changes_page(self, page_token, drive_id=None):
        self.log.append(("changes", drive_id, page_token))
        changes = [{"fileId": "file-1", "removed": True}] if drive_id is None else []
        return {"changes": changes, "newStartPageToken": "live", "nextPageToken": None}


@pytest.mark.asyncio
async def test_change_token_precedes_every_baseline_and_empty_page_is_followed():
    adapter = FakeAdapter()
    repository = MemoryRepository()
    result = await DriveInventoryService(
        adapter=adapter, repository=repository, account_namespace="test"
    ).sync_all()
    assert adapter.log[:3] == [("start", "user", None), ("drives",), ("start", "drive", "shared-1")]
    assert [(x.corpus_key, x.baseline_pages, x.change_pages) for x in result] == [
        ("user", 2, 1),
        ("drive:shared-1", 1, 1),
    ]
    assert repository.observed == [("user", "file-1")]
    assert repository.changes == [("user", "file-1", True)]


@pytest.mark.asyncio
async def test_network_failure_resumes_from_persisted_start_token():
    adapter = FakeAdapter(fail_once=True)
    repository = MemoryRepository()
    service = DriveInventoryService(
        adapter=adapter, repository=repository, account_namespace="test"
    )
    with pytest.raises(OSError):
        await service.sync_all()
    await service.sync_all()
    assert len([event for event in adapter.log if event == ("start", "user", None)]) == 1
    assert repository.checkpoints[("test", "user")].change_token == "live"


@pytest.mark.asyncio
async def test_incomplete_search_does_not_commit_page():
    adapter = FakeAdapter()
    repository = MemoryRepository()

    async def incomplete(*args, **kwargs):
        return {
            "files": [{"id": "x", "name": "X"}],
            "incompleteSearch": True,
            "nextPageToken": None,
        }

    adapter.list_files_page = incomplete
    with pytest.raises(RuntimeError, match="incompleteSearch"):
        await DriveInventoryService(
            adapter=adapter, repository=repository, account_namespace="test"
        ).sync_all()
    assert repository.observed == []
    assert not repository.checkpoints[("test", "user")].baseline_complete


@pytest.mark.asyncio
async def test_shared_drive_removal_without_file_id_is_not_dropped():
    adapter = FakeAdapter()
    repository = MemoryRepository()

    async def drive_change(token, drive_id=None):
        return {
            "changes": [{"changeType": "drive", "driveId": "shared-1", "removed": True}],
            "newStartPageToken": "live",
            "nextPageToken": None,
        }

    adapter.list_changes_page = drive_change
    await DriveInventoryService(
        adapter=adapter, repository=repository, account_namespace="test"
    ).sync_all()
    assert repository.changes == [("user", None, True), ("drive:shared-1", None, True)]
