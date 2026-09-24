from __future__ import annotations

from copy import deepcopy
from typing import Any

from archontos.domain.contracts import RuleEvaluationResult
from archontos.domain.enums import DecisionOutcome


class RuleEvaluationError(ValueError):
    pass


class MissingRuleFactError(RuleEvaluationError):
    pass


def _get_var(path: str, facts: dict[str, Any], default: Any = None) -> Any:
    current: Any = facts
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _resolve(value: Any, facts: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"var"}:
        var = value["var"]
        if isinstance(var, list):
            path = var[0]
            default = var[1] if len(var) > 1 else None
            return _get_var(path, facts, default)
        return _get_var(str(var), facts)
    if isinstance(value, dict) and set(value) == {"literal"}:
        return value["literal"]
    if isinstance(value, list):
        return [_resolve(item, facts) for item in value]
    return value


def evaluate_expr(expr: Any, facts: dict[str, Any]) -> Any:
    if not isinstance(expr, dict):
        return _resolve(expr, facts)
    if len(expr) != 1:
        return {key: evaluate_expr(value, facts) for key, value in expr.items()}

    op, args = next(iter(expr.items()))
    if op in {"var", "literal"}:
        return _resolve(expr, facts)

    args_list = args if isinstance(args, list) else [args]

    if op in {"and", "all"}:
        values = [evaluate_expr(item, facts) for item in args_list]
        if any(value is None for value in values):
            raise MissingRuleFactError(f"missing operand for rule operator: {op}")
        return all(bool(value) for value in values)
    if op in {"or", "any"}:
        values = [evaluate_expr(item, facts) for item in args_list]
        if any(value is None for value in values):
            raise MissingRuleFactError(f"missing operand for rule operator: {op}")
        return any(bool(value) for value in values)
    if op == "not":
        value = evaluate_expr(args_list[0], facts)
        if value is None:
            raise MissingRuleFactError("missing operand for rule operator: not")
        return not bool(value)

    resolved = [
        evaluate_expr(item, facts) if isinstance(item, dict) else _resolve(item, facts)
        for item in args_list
    ]
    if op in {"==", "!=", ">=", "<=", ">", "<", "in"} and any(
        value is None for value in resolved
    ):
        raise MissingRuleFactError(f"missing operand for rule operator: {op}")

    if op == "==":
        return resolved[0] == resolved[1]
    if op == "!=":
        return resolved[0] != resolved[1]
    if op == ">=":
        return resolved[0] >= resolved[1]
    if op == "<=":
        return resolved[0] <= resolved[1]
    if op == ">":
        return resolved[0] > resolved[1]
    if op == "<":
        return resolved[0] < resolved[1]
    if op == "in":
        return resolved[0] in resolved[1]

    raise RuleEvaluationError(f"Unsupported rule operator: {op}")


def _render(value: Any, facts: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"var"}:
            return _resolve(value, facts)
        return {key: _render(item, facts) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, facts) for item in value]
    return value


def _matches_scope(applicability: dict[str, Any], facts: dict[str, Any]) -> bool:
    jurisdictions = applicability.get("jurisdiction") or []
    if jurisdictions:
        current = _get_var("context.jurisdiction", facts)
        if current is None:
            raise MissingRuleFactError("missing context.jurisdiction")
        if current not in jurisdictions:
            return False

    use_groups = applicability.get("building_use_groups") or []
    if use_groups:
        current_use = _get_var("building.use_group", facts)
        if current_use is None:
            raise MissingRuleFactError("missing building.use_group")
        if current_use not in use_groups:
            return False

    conditions = applicability.get("conditions") or []
    condition_values = [evaluate_expr(condition, facts) for condition in conditions]
    if any(value is None for value in condition_values):
        raise MissingRuleFactError("missing applicability condition fact")
    return all(bool(value) for value in condition_values)


def evaluate_rule(rule_document: dict[str, Any], facts: dict[str, Any]) -> RuleEvaluationResult:
    doc = deepcopy(rule_document)
    applicability = doc.get("applicability", {})
    try:
        if applicability and not _matches_scope(applicability, facts):
            return RuleEvaluationResult(
                applicable=False,
                outcome=None,
                reason="Rule not applicable",
            )
    except MissingRuleFactError as exc:
        return RuleEvaluationResult(
            applicable=True,
            outcome=DecisionOutcome.REVIEW,
            reason="Insufficient facts to determine rule applicability",
            details={"error": str(exc)},
        )

    working_facts = deepcopy(facts)
    try:
        for exception in doc.get("exceptions", []):
            condition = exception.get("condition")
            if condition and evaluate_expr(condition, working_facts):
                for dotted_key, value in (exception.get("override") or {}).items():
                    target = working_facts
                    parts = dotted_key.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = value
    except MissingRuleFactError as exc:
        return RuleEvaluationResult(
            applicable=True,
            outcome=DecisionOutcome.REVIEW,
            reason="Insufficient facts to evaluate rule exceptions",
            details={"error": str(exc)},
        )

    body = doc.get("rule", doc)
    condition = body.get("if")
    if condition is None:
        raise RuleEvaluationError("Rule must contain an 'if' expression")

    try:
        condition_matches = bool(evaluate_expr(condition, working_facts))
    except MissingRuleFactError as exc:
        return RuleEvaluationResult(
            applicable=True,
            outcome=DecisionOutcome.REVIEW,
            reason="Insufficient facts to evaluate rule",
            details={"error": str(exc)},
        )

    branch = body.get("then") if condition_matches else body.get("else")
    if not isinstance(branch, dict):
        raise RuleEvaluationError("Rule branch must be an object")

    rendered = _render(branch, working_facts)
    if "PASS" in rendered:
        payload = rendered.get("PASS")
        details = payload if isinstance(payload, dict) else {"value": payload}
        return RuleEvaluationResult(applicable=True, outcome=DecisionOutcome.PASS, details=details)
    if "FAIL" in rendered:
        payload = rendered.get("FAIL")
        details = payload if isinstance(payload, dict) else {"value": payload}
        reason = details.get("reason") if isinstance(details, dict) else None
        return RuleEvaluationResult(
            applicable=True, outcome=DecisionOutcome.FAIL, reason=reason, details=details
        )
    if "REVIEW" in rendered:
        payload = rendered.get("REVIEW")
        details = payload if isinstance(payload, dict) else {"value": payload}
        reason = details.get("reason") if isinstance(details, dict) else None
        return RuleEvaluationResult(
            applicable=True, outcome=DecisionOutcome.REVIEW, reason=reason, details=details
        )
    raise RuleEvaluationError("Rule branch must contain PASS, FAIL or REVIEW")
