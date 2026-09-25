"""Run a resumable Drive metadata scan with an existing read-only OAuth token.

Create/apply db/migrations/004_drive_inventory.sql first. The token is supplied
via environment and is never written to the database or printed. This entry
point does not request or refresh credentials and cannot grant OAuth scopes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx

from archontos.db.session import get_session_factory
from archontos.ingestion.drive.adapter import GoogleDriveAdapter
from archontos.ingestion.drive.persistence import DriveInventoryRepository
from archontos.ingestion.drive.service import DriveInventoryService


async def run(account_namespace: str, max_pages: int) -> None:
    token = os.environ.get("ARCHONTOS_DRIVE_ACCESS_TOKEN")
    if not token:
        raise SystemExit("ARCHONTOS_DRIVE_ACCESS_TOKEN is required")
    if max_pages < 1:
        raise SystemExit("--max-pages must be positive")
    async with httpx.AsyncClient(timeout=30.0) as client:
        service = DriveInventoryService(
            adapter=GoogleDriveAdapter(client, token),
            repository=DriveInventoryRepository(get_session_factory()),
            account_namespace=account_namespace,
            max_pages_per_corpus=max_pages,
        )
        results = await service.sync_all()
    # File names, IDs, bearer tokens, and page tokens are deliberately omitted.
    print(json.dumps({"corpora": len(results),
                      "baseline_pages": sum(x.baseline_pages for x in results),
                      "change_pages": sum(x.change_pages for x in results),
                      "observations": sum(x.observed_files for x in results),
                      "removal_events": sum(x.removed_events for x in results)}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory Drive metadata in ArchOntos")
    parser.add_argument("--account-namespace", required=True,
                        help="Stable opaque account identifier, not an email address")
    parser.add_argument("--max-pages", type=int, default=100_000)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.account_namespace, arguments.max_pages))


if __name__ == "__main__":
    main()
