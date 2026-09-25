"""Read-only Google Drive v3 metadata adapter.

The caller owns OAuth token refresh and the injected ``httpx.AsyncClient``.
Do not record tokens, response bodies, or full HTTP exceptions in logs: Drive
metadata can contain private document names and authorization information.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import httpx


Corpus = Literal["user", "drive"]

FILE_FIELDS = (
    "id,name,mimeType,parents,driveId,trashed,explicitlyTrashed,"
    "createdTime,modifiedTime,md5Checksum,sha256Checksum,size,version,"
    "description,resourceKey,shortcutDetails(targetId,targetMimeType,targetResourceKey),"
    "owners(emailAddress,permissionId),capabilities(canDownload),"
    "permissionIds,shared"
)
FILES_LIST_FIELDS = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"
CHANGES_LIST_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(changeType,fileId,driveId,time,removed,"
    f"file({FILE_FIELDS}))"
)


class DriveAdapterError(RuntimeError):
    """Base error with no response body or authorization headers."""


class DriveAPIError(DriveAdapterError):
    """A Drive HTTP or network failure; status is None for transport failures."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        self.retryable = status_code is None or status_code in (408, 429) or status_code >= 500
        message = (
            "Drive API transport failure"
            if status_code is None else f"Drive API HTTP {status_code}"
        )
        super().__init__(message)


class DriveResponseError(DriveAdapterError):
    """A successful response was incomplete or had an invalid shape."""


class GoogleDriveAdapter:
    """A minimal Drive metadata client; never downloads file contents.

    ``access_token`` may be a provider callable, allowing the owner to refresh
    its OAuth token between pages without re-creating the adapter.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        access_token: str | Callable[[], str],
        *,
        base_url: str = "https://www.googleapis.com/drive/v3",
    ) -> None:
        self.client = client
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        token = self.access_token() if callable(self.access_token) else self.access_token
        if not isinstance(token, str) or not token.strip():
            raise DriveAdapterError("A nonempty Drive access token is required")
        try:
            response = await self.client.get(
                f"{self.base_url}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.RequestError:
            raise DriveAPIError() from None
        if not 200 <= response.status_code < 300:
            raise DriveAPIError(response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise DriveResponseError("Drive API returned invalid JSON") from None
        if not isinstance(data, dict):
            raise DriveResponseError("Drive API response must be an object")
        return data

    @staticmethod
    def _token(data: dict[str, Any], name: str, *, required: bool = False) -> str | None:
        value = data.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip():
            raise DriveResponseError(f"Drive API {name} must be a nonempty string")
        return value

    @staticmethod
    def _entries(data: dict[str, Any], name: str) -> list[dict[str, Any]]:
        value = data.get(name, [])
        if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
            raise DriveResponseError(f"Drive API {name} must be an array of objects")
        return value

    @staticmethod
    def _corpus(corpus: Corpus, drive_id: str | None) -> None:
        if corpus not in ("user", "drive"):
            raise ValueError("corpus must be 'user' or 'drive'")
        if corpus == "drive" and (not isinstance(drive_id, str) or not drive_id.strip()):
            raise ValueError("drive corpus requires a drive_id")
        if corpus == "user" and drive_id is not None:
            raise ValueError("user corpus must not have a drive_id")

    async def list_drives(self) -> list[dict[str, Any]]:
        """Enumerate every shared drive visible to the authenticated identity."""
        drives: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str | int] = {
                "pageSize": 100,
                "fields": "nextPageToken,drives(id,name,hidden,createdTime)",
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("drives", params)
            entries = self._entries(data, "drives")
            for entry in entries:
                self._token(entry, "id", required=True)
            drives.extend(entries)
            page_token = self._token(data, "nextPageToken")
            if page_token is None:
                return drives
            if page_token in seen_tokens:
                raise DriveResponseError("Drive API repeated a drives page token")
            seen_tokens.add(page_token)

    async def start_page_token(self, corpus: Corpus, drive_id: str | None = None) -> str:
        """Capture a per-corpus change cursor before the initial file listing."""
        self._corpus(corpus, drive_id)
        params: dict[str, str | int] = {"supportsAllDrives": "true", "fields": "startPageToken"}
        if drive_id is not None:
            params["driveId"] = drive_id
        data = await self._get("changes/startPageToken", params)
        token = self._token(data, "startPageToken", required=True)
        assert token is not None
        return token

    async def list_files_page(
        self,
        corpus: Corpus,
        drive_id: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one page, including trashed files, without collapsing source IDs."""
        self._corpus(corpus, drive_id)
        if page_token is not None and (not isinstance(page_token, str) or not page_token.strip()):
            raise ValueError("page_token must be a nonempty string")
        params: dict[str, str | int] = {
            "corpora": corpus,
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": FILES_LIST_FIELDS,
        }
        # Drive v3 lists trashed files by default. An explicit trashed=false
        # filter here would silently omit metadata required by the inventory.
        if drive_id is not None:
            params["driveId"] = drive_id
        if page_token is not None:
            params["pageToken"] = page_token
        data = await self._get("files", params)
        incomplete = data.get("incompleteSearch", False)
        if not isinstance(incomplete, bool):
            raise DriveResponseError("Drive API incompleteSearch must be a boolean")
        if incomplete:
            raise DriveResponseError("Drive API reported incompleteSearch")
        entries = self._entries(data, "files")
        for entry in entries:
            self._token(entry, "id", required=True)
        return {
            "files": entries,
            "nextPageToken": self._token(data, "nextPageToken"),
            "incompleteSearch": False,
        }

    async def list_changes_page(
        self, page_token: str, drive_id: str | None = None
    ) -> dict[str, Any]:
        """Read one change page including removals and loss of access."""
        if not isinstance(page_token, str) or not page_token.strip():
            raise ValueError("page_token must be a nonempty string")
        if drive_id is not None and (not isinstance(drive_id, str) or not drive_id.strip()):
            raise ValueError("drive_id must be a nonempty string")
        params: dict[str, str | int] = {
            "pageToken": page_token,
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "includeRemoved": "true",
            "fields": CHANGES_LIST_FIELDS,
        }
        if drive_id is not None:
            params["driveId"] = drive_id
        data = await self._get("changes", params)
        entries = self._entries(data, "changes")
        for entry in entries:
            if not entry.get("fileId") and not entry.get("driveId"):
                raise DriveResponseError("Drive API change requires fileId or driveId")
            if "removed" in entry and not isinstance(entry["removed"], bool):
                raise DriveResponseError("Drive API removed must be a boolean")
            if "file" in entry and not isinstance(entry["file"], dict):
                raise DriveResponseError("Drive API change file must be an object")
        next_token = self._token(data, "nextPageToken")
        new_start = self._token(data, "newStartPageToken")
        if next_token is not None and new_start is not None:
            raise DriveResponseError("Drive API returned both nextPageToken and newStartPageToken")
        if next_token is None and new_start is None:
            raise DriveResponseError("Drive API omitted terminal newStartPageToken")
        return {
            "changes": entries,
            "nextPageToken": next_token,
            "newStartPageToken": new_start,
        }
