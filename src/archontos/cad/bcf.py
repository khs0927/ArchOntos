"""MVP-5 BCF 3.0 export of CAD issues (pure python: zipfile + xml.etree).

Layout of the zip (buildingSMART BCF-XML 3.0)::

    bcf.version                 <Version VersionId="3.0"/>
    project.bcfp                <ProjectInfo><Project ProjectId=..><Name/></Project>
    extensions.xml              topic types / statuses / priorities / labels used
    <topic-guid>/markup.bcf     one topic per issue
    <topic-guid>/viewpoint.bcfv components by IFC GlobalId, else an orthogonal top camera
                                framing the object's plan bbox (metres); none for drawing-level

DXF handles and SketchUp persistent ids cannot be addressed by IFC viewers, so they travel in
``Labels`` and ``ReferenceLinks`` (``archontos://`` URIs). The zip is byte-deterministic for
the same issues: fixed entry timestamps, sorted entries, guids derived from issue ids.
"""

from __future__ import annotations

import io
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - building XML only, nothing is parsed
import zipfile
from collections.abc import Iterable, Mapping
from typing import Any

from archontos.cad.dxf import CAD_NAMESPACE
from archontos.cad.issues import ifc_guids

BCF_VERSION = "3.0"
TOPIC_STATUS = {"open": "Open", "resolved": "Resolved", "stale": "Stale"}
TOPIC_TYPE = {"FAIL": "Error", "REVIEW": "Warning"}
PRIORITY = {"FAIL": "High", "REVIEW": "Normal"}
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)
_CAMERA_HEIGHT = 10.0  # metres above the plan for the orthogonal top view
_MIN_SCALE = 1.0


def topic_guid(issue_id: str) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, f"bcf-topic\n{issue_id}"))


def viewpoint_guid(issue_id: str) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, f"bcf-viewpoint\n{issue_id}"))


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _xml(root: ET.Element) -> bytes:
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _num(v: float) -> str:
    return format(round(float(v), 6), "g")


def _point(parent: ET.Element, tag: str, x: float, y: float, z: float) -> None:
    el = _sub(parent, tag)
    _sub(el, "X", _num(x))
    _sub(el, "Y", _num(y))
    _sub(el, "Z", _num(z))


def labels_for(issue: Mapping[str, Any]) -> list[str]:
    out = [
        f"rule:{issue['rule_version_id']}",
        f"jurisdiction:{issue.get('jurisdiction_code')}",
        f"outcome:{issue['outcome']}",
        f"origin:{issue.get('origin', 'check')}",
    ]
    if issue.get("object_kind"):
        out.append(f"kind:{issue['object_kind']}")
    out += [f"dxf_handle:{h}" for h in issue.get("entity_handles") or ()]
    for ref in issue.get("source_refs") or ():
        if ref["kind"] != "dxf_handle":
            out.append(f"{ref['kind']}:{ref['value']}")
    return sorted(set(out))


def reference_links_for(issue: Mapping[str, Any]) -> list[str]:
    rev = issue["revision_id"]
    out = [
        f"archontos://cad/issues/{issue['id']}",
        f"archontos://cad/checks/{issue['run_id']}",
        f"archontos://cad/revisions/{rev}",
    ]
    if issue.get("arch_object_id"):
        out.append(f"archontos://cad/revisions/{rev}/objects/{issue['arch_object_id']}")
    out += [
        f"archontos://cad/revisions/{rev}/entities/{h}" for h in issue.get("entity_handles") or ()
    ]
    for ref in issue.get("source_refs") or ():
        if ref["kind"] == "skp_persistent_id":
            out.append(f"archontos://sketchup/{rev}/persistent_id/{ref['value']}")
    if issue.get("diff_id"):
        out.append(f"archontos://cad/diffs/{issue['diff_id']}")
    return out


def _description(issue: Mapping[str, Any]) -> str:
    lines = [
        f"{issue['outcome']}: {issue.get('rule_title') or issue['rule_version_id']}",
        f"reason: {issue.get('reason') or '-'}",
        f"jurisdiction: {issue.get('jurisdiction_code')}",
        f"effective: {issue.get('valid_from')} .. {issue.get('valid_to') or 'open'}",
        f"reference date: {issue.get('reference_date')}",
        f"status: {issue.get('status')}",
    ]
    for loc in issue.get("evidence_locator") or ():
        lines.append(f"evidence: {loc}")
    return "\n".join(lines)


def markup_xml(issue: Mapping[str, Any], *, author: str, created_at: str) -> bytes:
    root = ET.Element("Markup")
    topic = _sub(
        root,
        "Topic",
        Guid=topic_guid(issue["id"]),
        TopicType=TOPIC_TYPE.get(issue["outcome"], "Issue"),
        TopicStatus=TOPIC_STATUS.get(issue.get("status", "open"), "Open"),
    )
    links = _sub(topic, "ReferenceLinks")
    for link in reference_links_for(issue):
        _sub(links, "ReferenceLink", link)
    _sub(topic, "Title", f"{issue.get('rule_title') or 'rule'} — {issue.get('label', '')}"[:255])
    _sub(topic, "Priority", PRIORITY.get(issue["outcome"], "Normal"))
    labels = _sub(topic, "Labels")
    for label in labels_for(issue):
        _sub(labels, "Label", label)
    _sub(topic, "CreationDate", created_at)
    _sub(topic, "CreationAuthor", author)
    _sub(topic, "Description", _description(issue))
    if _has_viewpoint(issue):
        vps = _sub(topic, "Viewpoints")
        vp = _sub(vps, "ViewPoint", Guid=viewpoint_guid(issue["id"]))
        _sub(vp, "Viewpoint", "viewpoint.bcfv")
    return _xml(root)


def _has_viewpoint(issue: Mapping[str, Any]) -> bool:
    return bool(ifc_guids(issue)) or issue.get("bbox") is not None


def viewpoint_xml(issue: Mapping[str, Any]) -> bytes | None:
    """Components by IFC GlobalId when the object has one, else a bbox top camera."""
    guids = ifc_guids(issue)
    bbox = issue.get("bbox")
    if not guids and bbox is None:
        return None
    root = ET.Element("VisualizationInfo", Guid=viewpoint_guid(issue["id"]))
    if guids:
        comps = _sub(root, "Components")
        sel = _sub(comps, "Selection")
        for g in guids:
            _sub(sel, "Component", IfcGuid=g)
        _sub(comps, "Visibility", DefaultVisibility="true")
    if bbox is not None:
        x0, y0, x1, y1 = (float(v) for v in bbox)
        cam = _sub(root, "OrthogonalCamera")
        _point(cam, "CameraViewPoint", (x0 + x1) / 2, (y0 + y1) / 2, _CAMERA_HEIGHT)
        _point(cam, "CameraDirection", 0, 0, -1)
        _point(cam, "CameraUpVector", 0, 1, 0)
        _sub(cam, "ViewToWorldScale", _num(max(x1 - x0, y1 - y0, _MIN_SCALE) * 1.5))
        _sub(cam, "AspectRatio", "1")
    return _xml(root)


def version_xml() -> bytes:
    return _xml(ET.Element("Version", VersionId=BCF_VERSION))


def project_xml(project_id: str, name: str) -> bytes:
    root = ET.Element("ProjectInfo")
    proj = _sub(root, "Project", ProjectId=project_id)
    _sub(proj, "Name", name)
    return _xml(root)


def extensions_xml(issues: list[Mapping[str, Any]]) -> bytes:
    root = ET.Element("Extensions")
    groups = (
        ("TopicTypes", "TopicType", sorted(set(TOPIC_TYPE.values()))),
        ("TopicStatuses", "TopicStatus", sorted(set(TOPIC_STATUS.values()))),
        ("Priorities", "Priority", sorted(set(PRIORITY.values()))),
        ("TopicLabels", "TopicLabel", sorted({lab for i in issues for lab in labels_for(i)})),
    )
    for group, item, values in groups:
        g = _sub(root, group)
        for v in values:
            _sub(g, item, v)
    return _xml(root)


def _write(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def export_bcf(
    issues: Iterable[Mapping[str, Any]],
    *,
    project_id: str,
    project_name: str = "ArchOntos CAD issues",
    author: str = "archontos",
) -> bytes:
    """BCF 3.0 zip bytes for ``issues`` (deterministic for the same input)."""
    items = sorted(issues, key=lambda i: str(i["id"]))
    entries: list[tuple[str, bytes]] = [
        ("bcf.version", version_xml()),
        ("project.bcfp", project_xml(project_id, project_name)),
        ("extensions.xml", extensions_xml(items)),
    ]
    for issue in items:
        folder = topic_guid(issue["id"])
        created = str(issue.get("created_at") or f"{issue.get('reference_date')}T00:00:00Z")
        entries.append(
            (f"{folder}/markup.bcf", markup_xml(issue, author=author, created_at=created))
        )
        vp = viewpoint_xml(issue)
        if vp is not None:
            entries.append((f"{folder}/viewpoint.bcfv", vp))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in sorted(entries):
            _write(zf, name, data)
    return buf.getvalue()
