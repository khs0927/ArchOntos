"""Read-only Google Drive v3 metadata adapter.

The caller owns OAuth token refresh and the injected ``httpx.AsyncClient``.
Do not record tokens, response bodies, or full HTTP exceptions in logs: Drive
metadata can contain private document names and authorization information.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
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
            if status_code is None
            else f"Drive API HTTP {status_code}"
        )
        super().__init__(message)


class DriveAuthError(DriveAdapterError):
    """No usable access token could be produced.

    Deliberately not a ``DriveAPIError``: ``DriveAPIError`` with a null status
    reports ``retryable`` True, which would classify a credential problem as a
    transient provider fault and spend the retry budget on it.
    """

    retryable = False


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
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        max_delay: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not math.isfinite(retry_backoff) or retry_backoff < 0:
            raise ValueError("retry_backoff must be a finite, non-negative number")
        if not math.isfinite(max_delay) or max_delay < retry_backoff:
            # max_delay must be able to hold the first backoff, otherwise the
            # cap truncates the schedule to a flat line and stops meaning
            # "ceiling".
            raise ValueError("max_delay must be finite and at least retry_backoff")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_delay = max_delay
        # Injectable so the retry policy is testable without real delays.
        self._sleep = sleep if sleep is not None else asyncio.sleep

    def _current_token(self) -> str:
        """Return a usable bearer token or raise, without leaking the cause.

        The provider is called inside a try so that whatever its exception
        carries (a token endpoint response body, an echoed Authorization
        header) cannot reach an error message. A provider that returns an empty
        or non-string value raises rather than sending a literal
        ``Bearer None``, which Drive would answer with a 401 that masks the
        real cause as an authentication failure.
        """
        try:
            source = self.access_token() if callable(self.access_token) else self.access_token
        except Exception:  # noqa: BLE001 - deliberate: the provider owns the detail
            # Deliberately broad and deliberately detail-free: the provider owns
            # whatever the failure carries, and that may be a token endpoint
            # response body. Chaining with `from None` keeps it out of the
            # rendered traceback.
            raise DriveAuthError("Drive access token could not be obtained") from None
        if not isinstance(source, str) or not source.strip():
            raise DriveAuthError("A nonempty Drive access token is required")
        return source

    def _retry_delay(self, attempt: int, headers: httpx.Headers | None) -> float:
        """Seconds to wait before the next attempt, bounded by ``max_delay``.

        A ``Retry-After`` header wins over the exponential schedule because the
        provider's own quota window is more accurate than a guess, but the
        header is capped so a buggy one cannot stall the run. The exponential
        schedule is capped by the same ceiling; without that, a large
        ``max_retries`` produced a delay measured in days.
        """
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    seconds = float(retry_after)
                except ValueError:
                    seconds = -1.0
                if math.isfinite(seconds) and seconds >= 0:
                    return min(seconds, self.max_delay)
        return min(self.retry_backoff * (2**attempt), self.max_delay)

    async def _get(
        self,
        path: str,
        params: dict[str, str | int],
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            # Resolved outside the request try on purpose: a credential problem
            # must not consume the transient-failure retry budget.
            token = self._current_token()

            failure: DriveAPIError | None = None
            headers: httpx.Headers | None = None
            try:
                response = await self.client.get(
                    f"{self.base_url}/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.RequestError:
                failure = DriveAPIError()
            else:
                if 200 <= response.status_code < 300:
                    break
                headers = response.headers
                failure = DriveAPIError(response.status_code)

            if not retryable or not failure.retryable or attempt >= self.max_retries:
                raise failure from None
            await self._sleep(self._retry_delay(attempt, headers))
            attempt += 1

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
        # Not retried on purpose. Every call mints a fresh, later cursor, so a
        # retry after a 429 or 503 would silently advance the baseline point and
        # skip every change in the intervening window. Losing a transient
        # failure is recoverable; losing that window is not.
        data = await self._get("changes/startPageToken", params, retryable=False)
        # _token(..., required=True) already raises, so this is a type narrowing
        # for the declared return, not a runtime check. An assert would vanish
        # under python -O and assert has no business in the request path.
        token = self._token(data, "startPageToken", required=True)
        if token is None:  # pragma: no cover - unreachable while required=True
            raise DriveResponseError("Drive did not return a startPageToken")
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
