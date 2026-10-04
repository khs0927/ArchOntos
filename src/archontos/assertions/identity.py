from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID


def normalize_assertion_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def assertion_key(
    *,
    evidence_span_id: UUID,
    natural_language: str,
    structured_payload: dict[str, Any],
    applies_to: list[dict[str, Any]] | None = None,
) -> str:
    canonical = {
        "evidence_span_id": str(evidence_span_id),
        "natural_language": normalize_assertion_text(natural_language),
        "structured_payload": structured_payload,
    }
    if applies_to:
        # Empty references keep pre-migration identities stable; a different subject scope is a new
        # unreviewed candidate rather than silently changing a previously approved assertion.
        canonical["applies_to"] = sorted(
            {
                json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for ref in applies_to
            }
        )
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
