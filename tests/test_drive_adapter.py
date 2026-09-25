"""Drive adapter contract tests; no Google requests or credentials."""

import asyncio

import httpx
import pytest

from archontos.ingestion.drive.adapter import (
    DriveAPIError,
    DriveResponseError,
    GoogleDriveAdapter,
)


def run(coro):
    return asyncio.run(coro)


def test_list_drives_follows_empty_intermediate_page_and_refreshes_token():
    requests = []
    tokens = iter(["private-first", "private-second", "private-third"])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        pages = {
            None: {"drives": [{"id": "drive-A", "name": "A"}], "nextPageToken": "p2"},
            "p2": {"drives": [], "nextPageToken": "p3"},
            "p3": {"drives": [{"id": "drive-B", "name": "B"}]},
        }
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GoogleDriveAdapter(client, lambda: next(tokens)).list_drives()

    assert [d["id"] for d in run(scenario())] == ["drive-A", "drive-B"]
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer private-first", "Bearer private-second", "Bearer private-third"
    ]
    assert all(r.url.params["fields"].startswith("nextPageToken,drives(") for r in requests)
    assert all(r.url.params["pageSize"] == "100" for r in requests)


def test_corpus_tokens_file_pages_and_removed_changes_preserve_page_cursors():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        params = request.url.params
        if path.endswith("/changes/startPageToken"):
            return httpx.Response(
                200, json={"startPageToken": f"start-{params.get('driveId', 'user')}"}
            )
        if path.endswith("/files"):
            if params.get("pageToken") == "second":
                return httpx.Response(200, json={"files": [{"id": "B", "trashed": True}]})
            return httpx.Response(200, json={"files": [], "nextPageToken": "second"})
        if path.endswith("/changes"):
            if params["pageToken"] == "initial":
                return httpx.Response(200, json={
                    "changes": [{"fileId": "A", "removed": True}],
                    "nextPageToken": "next",
                })
            return httpx.Response(200, json={
                "changes": [{"fileId": "B", "removed": False, "file": {"id": "B"}}],
                "newStartPageToken": "stable",
            })
        raise AssertionError(f"Unexpected path: {path}")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleDriveAdapter(client, "private-token")
            return (
                await adapter.start_page_token("user"),
                await adapter.start_page_token("drive", "drive-A"),
                await adapter.list_files_page("drive", "drive-A"),
                await adapter.list_files_page("drive", "drive-A", "second"),
                await adapter.list_changes_page("initial", "drive-A"),
                await adapter.list_changes_page("next", "drive-A"),
            )

    user_start, drive_start, first, second, changes, terminal = run(scenario())
    assert (user_start, drive_start) == ("start-user", "start-drive-A")
    assert first == {"files": [], "nextPageToken": "second", "incompleteSearch": False}
    assert second["files"] == [{"id": "B", "trashed": True}]
    assert changes["changes"] == [{"fileId": "A", "removed": True}]
    assert changes["nextPageToken"] == "next"
    assert terminal["newStartPageToken"] == "stable"
    assert terminal["nextPageToken"] is None
    assert all(r.headers["Authorization"] == "Bearer private-token" for r in requests)
    file_requests = [r for r in requests if r.url.path.endswith("/files")]
    assert all(r.url.params["corpora"] == "drive" for r in file_requests)
    assert all(r.url.params["driveId"] == "drive-A" for r in file_requests)
    assert all(r.url.params["supportsAllDrives"] == "true" for r in file_requests)
    assert all(r.url.params["includeItemsFromAllDrives"] == "true" for r in file_requests)
    assert all("trashed" in r.url.params["fields"] for r in file_requests)
    assert all("q" not in r.url.params for r in file_requests)  # Include the trash.
    change_requests = [r for r in requests if r.url.path.endswith("/changes")]
    assert all(r.url.params["includeRemoved"] == "true" for r in change_requests)
    assert all(r.url.params["supportsAllDrives"] == "true" for r in change_requests)
    assert all(r.url.params["includeItemsFromAllDrives"] == "true" for r in change_requests)
    assert "driveId" not in requests[0].url.params
    assert requests[1].url.params["driveId"] == "drive-A"


@pytest.mark.parametrize(
    "payload",
    [
        {"files": [], "incompleteSearch": True},
        {"files": "not-an-array"},
        {"files": [{"name": "no identity"}]},
        {"nextPageToken": ""},
        {"incompleteSearch": "false"},
    ],
)
def test_invalid_or_incomplete_file_pages_are_rejected(payload):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            return await GoogleDriveAdapter(client, "secret").list_files_page("user")

    with pytest.raises(DriveResponseError):
        run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"changes": []},  # Terminal page MUST have a cursor to avoid missed updates.
        {"changes": [{"removed": True}], "newStartPageToken": "cursor"},
        {"changes": [{"fileId": "x", "removed": "true"}], "newStartPageToken": "cursor"},
        {"changes": [], "nextPageToken": "more", "newStartPageToken": "end"},
    ],
)
def test_invalid_change_pages_are_rejected(payload):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            return await GoogleDriveAdapter(client, "secret").list_changes_page("cursor")

    with pytest.raises(DriveResponseError):
        run(scenario())


def test_http_errors_hide_response_content_and_bearer_token():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(403, json={"error": "private-file-name and token"})
        )) as client:
            return await GoogleDriveAdapter(client, "bearer-secret").list_drives()

    with pytest.raises(DriveAPIError) as caught:
        run(scenario())
    assert caught.value.status_code == 403
    assert caught.value.retryable is False
    assert "private-file-name" not in str(caught.value)
    assert "bearer-secret" not in str(caught.value)


def test_non_json_success_is_not_treated_as_an_empty_inventory():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="<html>not an API response</html>")
        )) as client:
            return await GoogleDriveAdapter(client, "secret").list_drives()

    with pytest.raises(DriveResponseError, match="invalid JSON"):
        run(scenario())


def test_shared_drive_change_without_file_id_keeps_its_drive_identity():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={
                "changes": [{"changeType": "drive", "driveId": "shared-A", "removed": True}],
                "newStartPageToken": "after",
            })
        )) as client:
            return await GoogleDriveAdapter(client, "secret").list_changes_page("before")

    assert run(scenario())["changes"] == [
        {"changeType": "drive", "driveId": "shared-A", "removed": True}
    ]


def test_repeated_drives_page_token_is_rejected():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"drives": [], "nextPageToken": "again"})
            )
        ) as client:
            return await GoogleDriveAdapter(client, "secret").list_drives()

    with pytest.raises(DriveResponseError, match="repeated"):
        run(scenario())


def test_invalid_corpus_rejected_before_network_call():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("request should not be sent"))
        ) as client:
            return await GoogleDriveAdapter(client, "secret").start_page_token("drive")

    with pytest.raises(ValueError, match="drive_id"):
        run(scenario())
