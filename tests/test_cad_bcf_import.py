"""BCF 3.0 import parsing: topics, comments and hostile-zip handling (pure, no database)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pytest

from archontos.cad.bcf import export_bcf, topic_guid
from archontos.cad.bcf_import import (
    BcfImportError,
    parse_bcf,
    status_note_body,
    status_note_guid,
)
from tests.test_cad_issues_bcf import _issues_for_export

VERSION = b'<?xml version="1.0"?><Version VersionId="3.0"/>'
C1 = "11111111-1111-4111-8111-111111111111"
C2 = "22222222-2222-4222-8222-222222222222"
T1 = "33333333-3333-4333-8333-333333333333"


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _markup(topic: str = T1, status: str = "Resolved", comments: str = "") -> bytes:
    return (
        f'<Markup><Topic Guid="{topic}" TopicStatus="{status}">'
        f"<ModifiedDate>2026-11-02T09:30:00+09:00</ModifiedDate>"
        f"<ModifiedAuthor>kim@example.com</ModifiedAuthor>"
        f"<Comments>{comments}</Comments></Topic></Markup>"
    ).encode()


def _comment(guid: str, text: str, author: str = "lee@example.com") -> str:
    return (
        f'<Comment Guid="{guid}"><Date>2026-11-03T01:00:00Z</Date>'
        f"<Author>{author}</Author><Comment>{text}</Comment></Comment>"
    )


def test_parses_status_and_comments():
    data = _zip(
        {
            "bcf.version": VERSION,
            f"{T1}/markup.bcf": _markup(comments=_comment(C2, "later") + _comment(C1, "first")),
        }
    )
    (topic,) = parse_bcf(data)
    assert topic.guid == T1 and topic.status == "Resolved"
    assert topic.modified_author == "kim@example.com"
    assert topic.modified_at == datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    assert {c.guid for c in topic.comments} == {C1, C2}
    assert status_note_guid(topic) == "status:Resolved:2026-11-02T00:30:00+00:00"
    assert "Resolved" in status_note_body(topic)


def test_bcf21_style_comment_under_markup_is_read():
    markup = (
        f'<Markup><Topic Guid="{T1}" TopicStatus="Open"/>{_comment(C1, "legacy")}</Markup>'
    ).encode()
    (topic,) = parse_bcf(_zip({"bcf.version": VERSION, f"{T1}/markup.bcf": markup}))
    assert [c.text for c in topic.comments] == ["legacy"]


def test_own_export_parses_back_with_topic_guids():
    issues = _issues_for_export()
    topics = parse_bcf(export_bcf(issues, project_id="P-5"))
    assert sorted(t.guid for t in topics) == sorted(topic_guid(i["id"]) for i in issues)
    assert {t.status for t in topics} == {"Open"}


_DTD = b'<!DOCTYPE x [<!ENTITY a "b">]><Markup><Topic Guid="x"/></Markup>'


@pytest.mark.parametrize(
    "entries",
    [
        {"bcf.version": VERSION, "../evil/markup.bcf": _markup()},
        {"bcf.version": VERSION, "/abs/markup.bcf": _markup()},
        {f"{T1}/markup.bcf": _markup()},  # no bcf.version
        {"bcf.version": b'<Version VersionId="2.1"/>'},
        {"bcf.version": VERSION, f"{T1}/markup.bcf": b"<Markup>"},
        {"bcf.version": VERSION, f"{T1}/markup.bcf": _DTD},
    ],
)
def test_hostile_or_invalid_zip_is_rejected(entries):
    with pytest.raises(BcfImportError):
        parse_bcf(_zip(entries))


def test_not_a_zip_and_oversize_markup_rejected():
    with pytest.raises(BcfImportError):
        parse_bcf(b"not a zip")
    huge = b"<Markup>" + b" " * (3 * 1024 * 1024) + b"</Markup>"
    with pytest.raises(BcfImportError, match="too large"):
        parse_bcf(_zip({"bcf.version": VERSION, f"{T1}/markup.bcf": huge}))


def test_non_markup_and_nested_entries_are_ignored():
    data = _zip(
        {
            "bcf.version": VERSION,
            "project.bcfp": b"<ProjectInfo/>",
            f"{T1}/viewpoint.bcfv": b"<VisualizationInfo/>",
            f"deep/{T1}/markup.bcf": _markup(),
        }
    )
    assert parse_bcf(data) == []
