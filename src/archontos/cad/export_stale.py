"""Stale edge export for Ontology-platform: ``archontos-stale-edges/1`` (file based, no network).

A CAD check result is *stale* when its revision is the base of a lineage diff that changed
something (``stale_by_diff_ids`` of the check API, docs/MVP3-REVISION-DIFF.md). Ontology links a
rule to a drawing subject with a ``subjectTo`` edge (``archontos-rule-export/1``) and marks such an
edge stale only by comparing byte hashes. This export tells Ontology which edges ArchOntos itself
already knows are superseded, with the diff ids as the reason.

Document shape (canonical JSON: sorted keys, UTF-8, no clock, no random ids)::

    {"schema": "archontos-stale-edges/1",
     "revision_id": "<uuid or null>",
     "edges": [{"result_id", "run_id", "rule_version_id", "rule_title", "outcome",
                "stale": true, "reason": "superseded_by_lineage_diff",
                "stale_by_diff_ids": [...],
                "applies_to": [<archontos-aec-subject-ref/1>, ...],
                "unresolved_object_ids": [...]}],
     "summary": {"edge_count": n, "subject_count": n, "unresolved_object_count": n},
     "sha256": "<hash of the canonical JSON without this key>"}

``applies_to`` has the same subject-ref shape as the rule export, taken from the object's stored
``subject_ref``. An object id without a stored subject ref is listed in ``unresolved_object_ids``
instead of being invented. Results that are not stale are never exported; edges are sorted by
``result_id``.

CLI (inputs are the JSON the API returned, either a list or ``{"items": [...]}``)::

    python -m archontos.cad.export_stale --results results.json --objects objects.json \\
        [--revision-id <uuid>] --out stale-edges.json

``results.json`` is ``GET /v1/cad/revisions/{id}/checks/results``, ``objects.json`` is
``GET /v1/cad/revisions/{id}/objects``. The same document is served by
``GET /v1/cad/revisions/{id}/checks/stale-edges``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

STALE_EDGES_SCHEMA = "archontos-stale-edges/1"
STALE_REASON = "superseded_by_lineage_diff"


class StaleExportError(ValueError):
    """Input is not a result/object list."""


def _items(data: Any, what: str) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise StaleExportError(f"{what} must be a list of objects or {{'items': [...]}}")
    return data


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def build_stale_edges(
    results: Any, objects: Any, *, revision_id: str | None = None
) -> dict[str, Any]:
    """Build the ``archontos-stale-edges/1`` document from API result and object lists."""
    refs: dict[str, dict[str, Any]] = {}
    for obj in _items(objects, "objects"):
        ref = obj.get("subject_ref")
        if obj.get("id") and isinstance(ref, dict):
            refs[str(obj["id"])] = ref
    edges: list[dict[str, Any]] = []
    for res in _items(results, "results"):
        diff_ids = sorted(str(d) for d in res.get("stale_by_diff_ids") or ())
        if not (res.get("stale") or diff_ids):
            continue
        object_ids = sorted({str(o) for o in res.get("arch_object_ids") or ()})
        applies = [refs[o] for o in object_ids if o in refs]
        edges.append(
            {
                "result_id": str(res["id"]),
                "run_id": str(res["run_id"]) if res.get("run_id") else None,
                "rule_version_id": str(res["rule_version_id"]),
                "rule_title": res.get("rule_title"),
                "outcome": res.get("outcome"),
                "stale": True,
                "reason": STALE_REASON,
                "stale_by_diff_ids": diff_ids,
                "applies_to": applies,
                "unresolved_object_ids": [o for o in object_ids if o not in refs],
            }
        )
    edges.sort(key=lambda e: e["result_id"])
    body: dict[str, Any] = {
        "schema": STALE_EDGES_SCHEMA,
        "revision_id": revision_id,
        "edges": edges,
        "summary": {
            "edge_count": len(edges),
            "subject_count": sum(len(e["applies_to"]) for e in edges),
            "unresolved_object_count": sum(len(e["unresolved_object_ids"]) for e in edges),
        },
    }
    out = dict(body)
    out["sha256"] = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    return out


def dumps(document: dict[str, Any]) -> str:
    """Deterministic file text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archontos.cad.export_stale")
    parser.add_argument("--results", required=True, help="checks/results API JSON")
    parser.add_argument("--objects", required=True, help="objects API JSON (with subject_ref)")
    parser.add_argument("--revision-id", default=None)
    parser.add_argument("--out", default=None, help="write here instead of stdout")
    args = parser.parse_args(argv)

    def read(path: str) -> Any:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    try:
        document = build_stale_edges(
            read(args.results), read(args.objects), revision_id=args.revision_id
        )
    except (StaleExportError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    text = dumps(document)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
