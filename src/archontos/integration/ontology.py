"""Evaluate ArchOntos rules against facts exported by the Ontology knowledge graph.

Ontology (``khs0927/Ontology``) exports one project as ``aec-facts-export/1``
(``aec operational kg-facts <project>`` or ``GET /v1/kg/projects/{key}/facts``): rule-engine
facts derived from parsed drawings, per-fact provenance, and ``archontos-aec-subject-ref/1``
references for the project and its drawings. This module

* validates that export (every subject ref through :class:`AecSubjectRef`),
* merges operator-supplied context facts the drawings cannot provide (jurisdiction, use group),
  refusing to override a drawing-derived value,
* evaluates a rule document with the fail-closed engine (absent facts -> ``REVIEW``), and
* returns the decision with ``applies_to`` subject refs and the provenance of every fact the rule
  read, plus an ``archontos-rule-export/1`` entry Ontology links back into its graph.

Pure functions, no database and no network. CLI::

    python -m archontos.integration.ontology --rule rule.json --facts facts.json \\
        --context '{"context": {"jurisdiction": "KR-26"}, "building": {"use_group": "공동주택"}}' \\
        [--rule-id R-1 --version-label v1 --title "직통계단"] [--export-links links.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from archontos.domain.contracts import AecSubjectRef
from archontos.rules.engine import RuleEvaluationError, evaluate_rule

FACTS_SCHEMA = "aec-facts-export/1"
EVALUATION_SCHEMA = "archontos-ontology-evaluation/1"
RULE_EXPORT_SCHEMA = "archontos-rule-export/1"
_MISSING = object()


class OntologyFactsError(ValueError):
    pass


def load_facts_export(data: Any) -> dict[str, Any]:
    """Validate an ``aec-facts-export/1`` document; subject refs must satisfy AecSubjectRef."""
    if not isinstance(data, dict) or data.get("schema") != FACTS_SCHEMA:
        raise OntologyFactsError(f"expected schema {FACTS_SCHEMA}")
    if not isinstance(data.get("facts"), dict):
        raise OntologyFactsError("facts must be an object")
    subjects = []
    for index, subject in enumerate(data.get("subjects") or []):
        try:
            ref = AecSubjectRef.model_validate((subject or {}).get("ref"))
        except ValidationError as exc:
            raise OntologyFactsError(f"subjects[{index}].ref: {exc.errors()[0]['msg']}") from exc
        subjects.append({**subject, "ref": ref.model_dump(mode="json")})
    return {**data, "subjects": subjects, "provenance": data.get("provenance") or {}}


def _get(path: str, facts: dict[str, Any]) -> Any:
    current: Any = facts
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    else:
        out[prefix] = value


def merge_context(facts: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict, list]:
    """Add operator facts; a key the drawings already provide is kept and reported as a conflict."""
    merged = deepcopy(facts)
    conflicts = []
    flat: dict[str, Any] = {}
    _flatten("", context or {}, flat)
    for path, value in flat.items():
        existing = _get(path, merged)
        if existing is not _MISSING:
            if existing != value:
                conflicts.append({"fact": path, "drawings": existing, "operator": value})
            continue
        target = merged
        parts = path.split(".")
        for part in parts[:-1]:
            nxt = target.get(part)
            if not isinstance(nxt, dict):
                nxt = target[part] = {}
            target = nxt
        target[parts[-1]] = value
    return merged, conflicts


def rule_vars(expr: Any) -> list[str]:
    """Fact paths a rule document reads (``var`` operands and the scope facts)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if set(node) == {"var"}:
                var = node["var"]
                path = var[0] if isinstance(var, list) and var else var
                if isinstance(path, str) and path not in found:
                    found.append(path)
                return
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(expr)
    scope = (expr or {}).get("applicability") or {} if isinstance(expr, dict) else {}
    if scope.get("jurisdiction") and "context.jurisdiction" not in found:
        found.append("context.jurisdiction")
    if scope.get("building_use_groups") and "building.use_group" not in found:
        found.append("building.use_group")
    return found


def evaluate_against_ontology(rule_document: dict[str, Any], export: dict[str, Any], *,
                              context: dict[str, Any] | None = None, rule_id: str | None = None,
                              version_label: str | None = None) -> dict[str, Any]:
    export = load_facts_export(export)
    facts, conflicts = merge_context(export["facts"], context)
    try:
        result = evaluate_rule(rule_document, facts)
    except RuleEvaluationError as exc:  # malformed rule: never a verdict
        raise OntologyFactsError(f"rule cannot be evaluated: {exc}") from exc
    used = rule_vars(rule_document)
    operator_paths: dict[str, Any] = {}
    _flatten("", context or {}, operator_paths)
    evidence = {}
    for path in used:
        if _get(path, export["facts"]) is not _MISSING:
            evidence[path] = {"source": "ontology", **(export["provenance"].get(path) or {})}
        elif path in operator_paths:
            evidence[path] = {"source": "operator"}
    return {
        "schema": EVALUATION_SCHEMA,
        "rule_id": rule_id,
        "version_label": version_label,
        "project_key": export.get("project_key"),
        "applicable": result.applicable,
        "outcome": result.outcome.value if result.outcome else None,
        "reason": result.reason,
        "details": result.details,
        "applies_to": [s["ref"] for s in export["subjects"] if s.get("type") == "Project"],
        "facts_read": used,
        "missing_facts": [p for p in used if _get(p, facts) is _MISSING],
        "fact_evidence": evidence,
        "context_conflicts": conflicts,
    }


def rule_export_entry(evaluation: dict[str, Any], *, title: str | None = None,
                      text: str | None = None, source: Any = None) -> dict[str, Any]:
    """One ``archontos-rule-export/1`` rule linking the evaluated rule to the project subjects."""
    if not evaluation.get("rule_id"):
        raise OntologyFactsError("rule_id is required to export a link")
    return {"rule_id": evaluation["rule_id"], "version_label": evaluation.get("version_label"),
            "title": title or evaluation["rule_id"], "text": text, "source": source,
            "outcome": evaluation.get("outcome"), "applies_to": evaluation["applies_to"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archontos.integration.ontology")
    parser.add_argument("--rule", required=True, help="Rule document JSON (DSL v1)")
    parser.add_argument("--facts", required=True, help="Ontology aec-facts-export/1 JSON")
    parser.add_argument("--context", default=None, help="Operator facts JSON (string or @file)")
    parser.add_argument("--rule-id", default=None)
    parser.add_argument("--version-label", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--export-links", default=None, help="Write archontos-rule-export/1 here")
    args = parser.parse_args(argv)

    def read(path: str) -> Any:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    context = None
    if args.context:
        raw = args.context
        context = read(raw[1:]) if raw.startswith("@") else json.loads(raw)
    try:
        evaluation = evaluate_against_ontology(read(args.rule), read(args.facts), context=context,
                                               rule_id=args.rule_id,
                                               version_label=args.version_label)
    except OntologyFactsError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n")
    if args.export_links:
        export = {"schema": RULE_EXPORT_SCHEMA,
                  "rules": [rule_export_entry(evaluation, title=args.title)]}
        with open(args.export_links, "w", encoding="utf-8") as fh:
            json.dump(export, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
