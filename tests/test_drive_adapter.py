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
        "Bearer private-first",
        "Bearer private-second",
        "Bearer private-third",
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
                return httpx.Response(
                    200,
                    json={
                        "changes": [{"fileId": "A", "removed": True}],
                        "nextPageToken": "next",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "changes": [{"fileId": "B", "removed": False, "file": {"id": "B"}}],
                    "newStartPageToken": "stable",
                },
            )
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
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, json={"error": "private-file-name and token"})
            )
        ) as client:
            return await GoogleDriveAdapter(client, "bearer-secret").list_drives()

    with pytest.raises(DriveAPIError) as caught:
        run(scenario())
    assert caught.value.status_code == 403
    assert caught.value.retryable is False
    assert "private-file-name" not in str(caught.value)
    assert "bearer-secret" not in str(caught.value)


def test_non_json_success_is_not_treated_as_an_empty_inventory():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="<html>not an API response</html>")
            )
        ) as client:
            return await GoogleDriveAdapter(client, "secret").list_drives()

    with pytest.raises(DriveResponseError, match="invalid JSON"):
        run(scenario())


def test_shared_drive_change_without_file_id_keeps_its_drive_identity():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "changes": [
                            {"changeType": "drive", "driveId": "shared-A", "removed": True}
                        ],
                        "newStartPageToken": "after",
                    },
                )
            )
        ) as client:
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


def _adapter_over(responses, **kwargs):
    """Build an adapter over a scripted response list and record its sleeps."""
    seen = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = calls["n"]
        calls["n"] += 1
        item = responses[min(index, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleDriveAdapter(
                client,
                "bearer",
                sleep=_record_sleep(seen),
                **kwargs,
            )
            return await adapter.list_drives(), calls["n"], list(seen)

    return scenario


def _record_sleep(seen):
    async def sleep(seconds):
        seen.append(seconds)

    return sleep


def test_transient_503_is_retried_and_the_scan_survives():
    """A 503 must consume the retry budget instead of killing the whole corpus."""
    scenario = _adapter_over(
        [
            httpx.Response(503, json={"error": "backendError"}),
            httpx.Response(200, json={"drives": [{"id": "drive-A", "name": "A"}]}),
        ]
    )
    drives, calls, sleeps = run(scenario())

    assert calls == 2
    assert [d["id"] for d in drives] == ["drive-A"]
    assert sleeps == [0.5]


def test_transient_429_is_retried_and_then_recovers():
    scenario = _adapter_over(
        [
            httpx.Response(429, json={"error": "rateLimitExceeded"}),
            httpx.Response(200, json={"drives": []}),
        ]
    )
    drives, calls, _sleeps = run(scenario())

    assert calls == 2
    assert drives == []


def test_transport_failure_is_retried_because_retryable_is_true_for_none_status():
    scenario = _adapter_over(
        [
            httpx.ConnectError("connection reset"),
            httpx.Response(200, json={"drives": []}),
        ]
    )
    _drives, calls, _sleeps = run(scenario())

    assert calls == 2


def test_retry_budget_is_bounded_and_the_final_error_still_carries_its_status():
    scenario = _adapter_over(
        [httpx.Response(503, json={"error": "backendError"})],
        max_retries=2,
    )
    with pytest.raises(DriveAPIError) as caught:
        run(scenario())

    # one initial attempt plus two retries, then give up
    assert caught.value.status_code == 503
    assert caught.value.retryable is True


def test_non_retryable_403_fails_immediately_without_sleeping():
    scenario = _adapter_over([httpx.Response(403, json={"error": "insufficientFilePermissions"})])
    with pytest.raises(DriveAPIError) as caught:
        run(scenario())

    assert caught.value.status_code == 403
    assert caught.value.retryable is False


def test_retry_backoff_grows_exponentially():
    with pytest.raises(DriveAPIError):
        run(
            _adapter_over(
                [httpx.Response(503, json={"error": "backendError"})],
                max_retries=3,
                retry_backoff=0.25,
            )()
        )

    seen: list[float] = []

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, json={}))
        ) as client:
            adapter = GoogleDriveAdapter(
                client,
                "bearer",
                max_retries=3,
                retry_backoff=0.25,
                sleep=_record_sleep(seen),
            )
            with pytest.raises(DriveAPIError):
                await adapter.list_drives()

    run(scenario())
    assert seen == [0.25, 0.5, 1.0]


def test_retry_after_header_overrides_the_backoff_schedule_and_is_capped():
    scenario = _adapter_over(
        [
            httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate"}),
            httpx.Response(200, json={"drives": []}),
        ]
    )
    _drives, _calls, sleeps = run(scenario())
    assert sleeps == [7.0]

    capped = _adapter_over(
        [
            httpx.Response(429, headers={"Retry-After": "99999"}, json={"error": "rate"}),
            httpx.Response(200, json={"drives": []}),
        ],
        max_retry_after=30.0,
    )
    _drives, _calls, capped_sleeps = run(capped())
    assert capped_sleeps == [30.0]


def test_malformed_retry_after_falls_back_to_the_backoff_schedule():
    scenario = _adapter_over(
        [
            httpx.Response(429, headers={"Retry-After": "soon"}, json={"error": "rate"}),
            httpx.Response(200, json={"drives": []}),
        ],
        retry_backoff=0.5,
    )
    _drives, _calls, sleeps = run(scenario())
    assert sleeps == [0.5]


def test_retries_do_not_leak_the_token_or_the_response_body():
    scenario = _adapter_over([httpx.Response(503, json={"error": "private-name-and-token"})])
    with pytest.raises(DriveAPIError) as caught:
        run(scenario())

    assert "private-name" not in str(caught.value)
    assert "bearer" not in str(caught.value)


def test_retry_budget_of_zero_disables_retrying_entirely():
    scenario = _adapter_over(
        [httpx.Response(503, json={"error": "backendError"})],
        max_retries=0,
    )
    with pytest.raises(DriveAPIError) as caught:
        run(scenario())
    assert caught.value.status_code == 503
