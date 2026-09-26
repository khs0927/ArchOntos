"""Contract tests for the transactional Drive repository without PostgreSQL.

The in-memory session checks transaction rollback and SQL writes. Production
uses SQLAlchemy's AsyncSession and PostgreSQL's row locks/unique constraints.
"""

import asyncio
import sys
import types
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

try:
    from sqlalchemy import text as _sqlalchemy_text  # noqa: F401
except ImportError:
    sys.modules["sqlalchemy"] = types.SimpleNamespace(text=lambda query: query)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archontos.ingestion.drive.persistence import (  # noqa: E402
    DriveChange,
    DriveCursorConflict,
    DriveInventoryRepository,
    DriveItemSnapshot,
    DriveReceiptMismatch,
)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def scalar_one(self):
        if len(self.rows) != 1:
            raise ValueError("expected one result")
        row = self.rows[0]
        return row["id"] if isinstance(row, dict) else row


class FakeDB:
    def __init__(self):
        corpus = uuid4()
        account = uuid4()
        self.data = {
            "corpus": {
                "id": corpus,
                "account_id": account,
                "corpus_key": "drive:d1",
                "baseline_scan_id": uuid4(),
                "start_token": "start",
                "baseline_complete": False,
                "baseline_page_token": None,
                "change_token": "start",
            },
            "items": {},
            "memberships": {},
            "baseline_receipts": {},
            "change_receipts": {},
            "observations": [],
            "events": [],
            "outbox": [],
            "access_observations": [],
        }
        self.fail_cas = False
        self.sessions = []

    def __call__(self):
        session = FakeSession(self)
        self.sessions.append(session)
        return session


class FakeSession:
    def __init__(self, db):
        self.db = db
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @asynccontextmanager
    async def begin(self):
        snapshot = deepcopy(self.db.data)
        try:
            yield
        except BaseException:
            self.db.data = snapshot
            raise

    async def execute(self, statement, params=None):
        query = " ".join(str(statement).split())
        p = params or {}
        data = self.db.data
        self.commands.append(query)
        if query.startswith("INSERT INTO drive_provider_account"):
            return FakeResult()
        if query.startswith("SELECT id FROM drive_provider_account"):
            return FakeResult([data["corpus"]["account_id"]])
        if query.startswith("INSERT INTO drive_corpus("):
            return FakeResult()
        if "FROM drive_corpus c JOIN drive_provider_account a" in query:
            return FakeResult([data["corpus"]])
        if "FROM drive_baseline_page_receipt" in query:
            key = (p["scan_id"], p["input_token"])
            receipt = data["baseline_receipts"].get(key)
            return FakeResult([receipt] if receipt else [])
        if "FROM drive_change_page_receipt" in query:
            key = (p["input_token"], p["digest"])
            receipt = data["change_receipts"].get(key)
            return FakeResult([receipt] if receipt else [])
        if "SELECT status, reason FROM drive_corpus_membership" in query:
            member = data["memberships"].get((p["corpus_id"], p["item_id"]))
            return FakeResult([member] if member else [])
        if "SELECT m.item_id, i.provider_file_id FROM drive_corpus_membership" in query:
            rows = []
            for (corpus, item_id), member in data["memberships"].items():
                if (
                    corpus == p["corpus_id"]
                    and member["status"] == "ACTIVE"
                    and member.get("last_seen_scan_id") != p["scan_id"]
                ):
                    rows.append(
                        {"item_id": item_id, "provider_file_id": data["items"][item_id]["file_id"]}
                    )
            return FakeResult(rows)
        if "SELECT m.corpus_id, m.item_id, i.provider_file_id" in query:
            rows = []
            for (corpus, item_id), member in data["memberships"].items():
                item = data["items"][item_id]
                if (
                    (
                        item["drive_id"] == p["drive_id"]
                        or (corpus == p["corpus_id"] and p["corpus_key"] == p["shared_corpus_key"])
                    )
                    and item["account_id"] == p["account_id"]
                    and member["status"] == "ACTIVE"
                ):
                    rows.append(
                        {
                            "corpus_id": corpus,
                            "item_id": item_id,
                            "provider_file_id": item["file_id"],
                        }
                    )
            return FakeResult(rows)
        if query.startswith("INSERT INTO drive_baseline_page_receipt"):
            data["baseline_receipts"][(p["scan_id"], p["input_token"])] = {
                "page_fingerprint": p["digest"],
                "output_page_token": p["output_token"],
                "is_last": p["is_last"],
            }
            return FakeResult()
        if query.startswith("INSERT INTO drive_change_page_receipt"):
            data["change_receipts"][(p["input_token"], p["digest"])] = {
                "page_fingerprint": p["digest"],
                "output_change_token": p["output_token"],
                "is_last": p["is_last"],
            }
            return FakeResult()
        if query.startswith("INSERT INTO drive_item"):
            found = next(
                (
                    key
                    for key, item in data["items"].items()
                    if item["account_id"] == p["account_id"] and item["file_id"] == p["file_id"]
                ),
                p["id"],
            )
            if found not in data["items"]:
                data["items"][found] = {
                    "account_id": p["account_id"],
                    "file_id": p["file_id"],
                    "drive_id": None,
                }
            if "drive_id" in p:
                data["items"][found]["drive_id"] = p["drive_id"]
            return FakeResult([found])
        if query.startswith("INSERT INTO drive_metadata_observation"):
            data["observations"].append(deepcopy(p))
            return FakeResult()
        if query.startswith("INSERT INTO drive_access_observation"):
            data["access_observations"].append(deepcopy(p))
            return FakeResult()
        if query.startswith("INSERT INTO drive_corpus_membership"):
            key = (p["corpus_id"], p["item_id"])
            previous = data["memberships"].get(key)
            data["memberships"][key] = {
                "status": p["status"],
                "reason": p["reason"],
                "last_seen_scan_id": p["scan_id"]
                if p["scan_id"] is not None
                else (previous.get("last_seen_scan_id") if previous else None),
            }
            return FakeResult()
        if query.startswith("INSERT INTO domain_event"):
            data["events"].append(deepcopy(p))
            return FakeResult()
        if query.startswith("INSERT INTO outbox_message"):
            data["outbox"].append(deepcopy(p))
            return FakeResult()
        if query.startswith("UPDATE drive_corpus SET baseline_page_token"):
            if self.db.fail_cas or data["corpus"]["baseline_page_token"] != p["expected"]:
                return FakeResult()
            data["corpus"]["baseline_page_token"] = p["next_token"]
            data["corpus"]["baseline_complete"] = p["is_last"]
            return FakeResult([deepcopy(data["corpus"])])
        if query.startswith("UPDATE drive_corpus SET change_token"):
            if self.db.fail_cas or data["corpus"]["change_token"] != p["expected"]:
                return FakeResult()
            data["corpus"]["change_token"] = p["next_token"]
            return FakeResult([deepcopy(data["corpus"])])
        raise AssertionError(f"Unhandled query: {query}")


class DrivePersistenceTests(unittest.TestCase):
    @staticmethod
    def item(file_id="f1", drive_id="d1"):
        return DriveItemSnapshot(
            id=file_id,
            name="Drawing.dwg",
            mime_type="application/acad",
            metadata={"driveId": drive_id},
        )

    def test_baseline_atomic_idempotent_and_metadata_only(self):
        async def scenario():
            db = FakeDB()
            repo = DriveInventoryRepository(db)
            item = self.item()
            result = await repo.commit_baseline_page(
                "account", "drive:d1", None, None, [item], is_last=True
            )
            self.assertTrue(result.baseline_complete)
            self.assertEqual(result.change_token, "start")
            self.assertEqual(len(db.data["items"]), 1)
            self.assertEqual(len(db.data["observations"]), 1)
            before = deepcopy(db.data)
            await repo.commit_baseline_page("account", "drive:d1", None, None, [item], is_last=True)
            self.assertEqual(db.data, before)
            with self.assertRaises(DriveReceiptMismatch):
                await repo.commit_baseline_page(
                    "account", "drive:d1", None, None, [self.item("f2")], is_last=True
                )
            self.assertFalse(
                any(
                    "INSERT INTO artifact" in command
                    for session in db.sessions
                    for command in session.commands
                )
            )

        asyncio.run(scenario())

    def test_begin_scan_preserves_partial_start_token_and_page_cursor(self):
        async def scenario():
            db = FakeDB()
            db.data["corpus"]["baseline_page_token"] = "resume-page"
            repo = DriveInventoryRepository(db)
            checkpoint = await repo.begin_scan("account", "drive:d1", "new-token")
            self.assertEqual(checkpoint.start_token, "start")
            self.assertEqual(checkpoint.change_token, "start")
            self.assertEqual(checkpoint.page_token, "resume-page")

        asyncio.run(scenario())

    def test_removed_change_blocks_and_emits_matching_outbox(self):
        async def scenario():
            db = FakeDB()
            repo = DriveInventoryRepository(db)
            await repo.commit_baseline_page(
                "account", "user", None, None, [self.item()], is_last=True
            )
            result = await repo.commit_change_page(
                "account",
                "user",
                "start",
                "next",
                [DriveChange(file_id="f1", removed=True)],
                is_last=True,
            )
            self.assertEqual(result.change_token, "next")
            membership = next(iter(db.data["memberships"].values()))
            self.assertEqual(membership["status"], "BLOCKED")
            self.assertEqual(membership["reason"], "REMOVED_OR_INACCESSIBLE")
            self.assertEqual(len(db.data["events"]), 1)
            self.assertEqual(db.data["events"][0]["event_id"], db.data["outbox"][0]["event_id"])
            before = deepcopy(db.data)
            await repo.commit_change_page(
                "account",
                "user",
                "start",
                "next",
                [DriveChange(file_id="f1", removed=True)],
                is_last=True,
            )
            self.assertEqual(db.data, before)

        asyncio.run(scenario())

    def test_baseline_absence_blocks_only_this_corpus_membership(self):
        async def scenario():
            db = FakeDB()
            corpus = db.data["corpus"]
            item_id, other_corpus = uuid4(), uuid4()
            db.data["items"][item_id] = {
                "account_id": corpus["account_id"],
                "file_id": "old-file",
                "drive_id": "d1",
            }
            active = {"status": "ACTIVE", "reason": None, "last_seen_scan_id": uuid4()}
            db.data["memberships"][(corpus["id"], item_id)] = deepcopy(active)
            db.data["memberships"][(other_corpus, item_id)] = deepcopy(active)
            repo = DriveInventoryRepository(db)
            await repo.commit_baseline_page("account", "drive:d1", None, None, [], is_last=True)
            own = db.data["memberships"][(corpus["id"], item_id)]
            other = db.data["memberships"][(other_corpus, item_id)]
            self.assertEqual((own["status"], own["reason"]), ("BLOCKED", "NOT_OBSERVED_BASELINE"))
            self.assertEqual(other["status"], "ACTIVE")
            self.assertEqual(len(db.data["events"]), len(db.data["outbox"]))

        asyncio.run(scenario())

    def test_failed_compare_and_swap_rolls_back_receipt_observations_and_events(self):
        async def scenario():
            db = FakeDB()
            repo = DriveInventoryRepository(db)
            await repo.commit_baseline_page(
                "account", "user", None, None, [self.item()], is_last=True
            )
            before = deepcopy(db.data)
            db.fail_cas = True
            with self.assertRaises(DriveCursorConflict):
                await repo.commit_change_page(
                    "account",
                    "user",
                    "start",
                    "next",
                    [DriveChange(file_id="f1", removed=True)],
                    is_last=True,
                )
            self.assertEqual(db.data, before)

        asyncio.run(scenario())

    def test_drive_level_loss_without_file_id_and_unchanged_token_poll(self):
        async def scenario():
            db = FakeDB()
            repo = DriveInventoryRepository(db)
            await repo.commit_baseline_page(
                "account", "drive:d1", None, None, [self.item()], is_last=True
            )
            await repo.commit_change_page("account", "drive:d1", "start", "start", [], is_last=True)
            result = await repo.commit_change_page(
                "account",
                "drive:d1",
                "start",
                "next",
                [DriveChange(file_id=None, removed=True, change_type="drive", drive_id="d1")],
                is_last=True,
            )
            self.assertEqual(result.change_token, "next")
            self.assertEqual(len(db.data["change_receipts"]), 2)
            self.assertEqual(len(db.data["access_observations"]), 1)
            self.assertEqual(next(iter(db.data["memberships"].values()))["status"], "BLOCKED")
            self.assertEqual(len(db.data["events"]), len(db.data["outbox"]))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
