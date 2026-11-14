"""BCF 3.0 import: topic statuses and comments become issue notes (pure parsing).

Only ``markup.bcf`` files are read. The zip is treated as hostile: bounded entry count and
sizes (bounded *reads*, not just the declared sizes), no path traversal, and XML is parsed with
``defusedxml`` (DTDs, entities and external references are refused). Nothing here creates
issues; topics are matched to existing issues by their deterministic GUID
(:func:`archontos.cad.bcf.topic_guid`) by the caller.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree.ElementTree import Element  # noqa: S405 - type only; parsing uses defusedxml

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_MARKUP_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TOPICS = 2000
MAX_COMMENTS_PER_TOPIC = 500
_MARKUP_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})/markup\.bcf$")


class BcfImportError(ValueError):
    """The upload is not an acceptable BCF 3.0 zip (client error)."""


@dataclass(frozen=True, slots=True)
class BcfComment:
    guid: str
    author: str | None
    date: datetime | None
    text: str


@dataclass(frozen=True, slots=True)
class BcfTopic:
    guid: str
    status: str | None
    modified_at: datetime | None
    modified_author: str | None
    comments: tuple[BcfComment, ...] = field(default_factory=tuple)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            value = (child.text or "").strip()
            return value or None
    return None


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _guid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_member(name: str) -> bool:
    return not (
        name.startswith(("/", "\\"))
        or "\\" in name
        or ":" in name
        or any(part in ("", "..", ".") for part in name.rstrip("/").split("/"))
    )


def _read_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with zf.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise BcfImportError(f"zip entry {info.filename!r} is too large")
    return data


def _parse_xml(data: bytes, what: str) -> Element:
    try:
        return SafeET.fromstring(data, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException, ValueError) as exc:
        raise BcfImportError(f"{what} is not safe, well-formed XML") from exc


def _comments(topic_el: Element, markup_el: Element) -> list[BcfComment]:
    # BCF 3.0 nests <Comments> in <Topic>; BCF 2.1 put <Comment> directly under <Markup>.
    found: list[Element] = []
    for container in (topic_el, markup_el):
        for child in container:
            if _local(child.tag) == "Comments":
                found += [c for c in child if _local(c.tag) == "Comment"]
            elif container is markup_el and _local(child.tag) == "Comment":
                found.append(child)
    out: list[BcfComment] = []
    for el in found[:MAX_COMMENTS_PER_TOPIC]:
        guid = _guid(el.get("Guid"))
        body = _child_text(el, "Comment")
        if guid is None or body is None:
            continue
        out.append(
            BcfComment(
                guid=guid,
                author=_child_text(el, "Author"),
                date=_date(_child_text(el, "Date")),
                text=body,
            )
        )
    return out


def parse_markup(data: bytes) -> BcfTopic | None:
    root = _parse_xml(data, "markup.bcf")
    topic = next((c for c in root if _local(c.tag) == "Topic"), None)
    guid = _guid(topic.get("Guid")) if topic is not None else None
    if topic is None or guid is None:
        return None
    return BcfTopic(
        guid=guid,
        status=topic.get("TopicStatus") or None,
        modified_at=_date(_child_text(topic, "ModifiedDate")),
        modified_author=_child_text(topic, "ModifiedAuthor"),
        comments=tuple(_comments(topic, root)),
    )


def parse_bcf(data: bytes) -> list[BcfTopic]:
    """Topics of a BCF zip, sorted by guid. Raises :class:`BcfImportError` on a bad upload."""
    if len(data) > MAX_ZIP_BYTES:
        raise BcfImportError("BCF upload exceeds 20 MiB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BcfImportError("not a zip file") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BcfImportError("too many zip entries")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise BcfImportError("zip expands to more than 64 MiB")
        for info in infos:
            if not _safe_member(info.filename):
                raise BcfImportError(f"unsafe zip entry name {info.filename!r}")
            if info.flag_bits & 0x1:
                raise BcfImportError("encrypted zip entries are not supported")
        by_name = {i.filename: i for i in infos}
        version = by_name.get("bcf.version")
        if version is None:
            raise BcfImportError("bcf.version is missing")
        root = _parse_xml(_read_bounded(zf, version, MAX_MARKUP_BYTES), "bcf.version")
        if not (root.get("VersionId") or "").startswith("3."):
            raise BcfImportError("only BCF 3.x is supported")
        topics: dict[str, BcfTopic] = {}
        markups = sorted(
            (i for i in infos if _MARKUP_RE.match(i.filename)), key=lambda i: i.filename
        )
        if len(markups) > MAX_TOPICS:
            raise BcfImportError("too many topics")
        for info in markups:
            topic = parse_markup(_read_bounded(zf, info, MAX_MARKUP_BYTES))
            if topic is not None:
                topics.setdefault(topic.guid, topic)
    return [topics[g] for g in sorted(topics)]


def status_note_guid(topic: BcfTopic) -> str | None:
    """Idempotency key of a topic's status observation (status + its modification stamp)."""
    if not topic.status:
        return None
    stamp = topic.modified_at.astimezone(UTC).isoformat() if topic.modified_at else "-"
    return f"status:{topic.status}:{stamp}"[:200]


def status_note_body(topic: BcfTopic) -> str:
    who = f" by {topic.modified_author}" if topic.modified_author else ""
    return f"BCF import: topic status '{topic.status}'{who}"
