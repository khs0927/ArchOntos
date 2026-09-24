"""Offline reference rules, not a deployed Drive worker or legal engine."""
import hashlib
import json
import math
import re


def archive_member_id(snapshot_id, ordinal, path):
    if not snapshot_id or type(ordinal) is not int or ordinal < 0:
        raise ValueError("snapshot and non-negative entry ordinal required")
    path = path.replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError("absolute or empty archive path")
    parts = path.split("/")
    if any(p == ".." for p in parts) or "\x00" in path:
        raise ValueError("unsafe archive path")
    normalized = "/".join(p for p in parts if p not in ("", "."))
    if not normalized:
        raise ValueError("empty member path")
    key = json.dumps([snapshot_id, ordinal, normalized], ensure_ascii=False,
                     separators=(",", ":")).encode()
    return "urn:arch:archive-member:" + hashlib.sha256(key).hexdigest()


def observed_coverage(rows):
    resolved = {"ARCHITECTURE", "SUPPORTING", "UNRELATED_METADATA_ONLY"}
    relevant = [r for r in rows if r["relevance"] in {"ARCHITECTURE", "SUPPORTING"}]
    complete = sum(r.get("extraction") == "COMPLETE" for r in relevant)
    return {
        "observed": len(rows),
        "classification_resolved": sum(r["relevance"] in resolved for r in rows),
        "review_pending": sum(r["relevance"] not in resolved for r in rows),
        "relevant": len(relevant),
        "content_complete": complete,
        "approved_exceptions": sum(bool(r.get("exception_approved")) for r in relevant),
        "content_complete_ratio": complete / len(relevant) if relevant else None,
        "global_inventory_ratio": None,
    }


def to_metres(value, insunits):
    scales = {1: 0.0254, 4: 0.001, 6: 1.0}
    if insunits not in scales or not math.isfinite(value):
        raise ValueError("unknown unit or invalid measurement")
    return value * scales[insunits]


def minimum_check(lower, upper, threshold, *, applicable,
                  measurement_verified, uncertainty_kind):
    """Numerical candidate only. Never a professional approval."""
    if applicable is False:
        return "NOT_APPLICABLE"
    if applicable is not True:
        return "NEEDS_REVIEW"
    if not measurement_verified:
        return "UNMEASURABLE"
    if uncertainty_kind != "DETERMINISTIC_BOUND":
        return "NEEDS_REVIEW"
    vals = (lower, upper, threshold)
    if any(not isinstance(v, (int, float)) or isinstance(v, bool)
           or not math.isfinite(v) for v in vals) or lower > upper:
        return "UNMEASURABLE"
    if lower >= threshold:
        return "PASS_CANDIDATE"
    if upper < threshold:
        return "FAIL_CANDIDATE"
    return "NEEDS_REVIEW"


def may_use_content(access_state, fresh_check, principal_authorized):
    return access_state == "ACCESSIBLE" and fresh_check is True and principal_authorized is True
