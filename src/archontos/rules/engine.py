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
        values = [
            _reject_absent(evaluate_expr(item, facts), f"{op}[{index}]")
            for index, item in enumerate(args_list)
        ]
        return all(bool(value) for value in values)
    if op in {"or", "any"}:
        values = [
            _reject_absent(evaluate_expr(item, facts), f"{op}[{index}]")
            for index, item in enumerate(args_list)
        ]
        return any(bool(value) for value in values)
    if op == "not":
        return not bool(_reject_absent(evaluate_expr(args_list[0], facts), "not"))

    resolved = [
        _reject_absent(
            evaluate_expr(item, facts) if isinstance(item, dict) else _resolve(item, facts),
            f"{op}[{index}]",
        )
        for index, item in enumerate(args_list)
    ]

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


def _reject_absent(value: Any, where: str) -> Any:
    """Fail closed when a resolved expression still holds an absent fact.

    ``_get_var`` returns ``None`` for a fact that is not present, and a list or
    dict literal keeps that ``None`` inside the structure. Handing either to
    ``bool()`` turns a missing fact into a definitive verdict, so every
    resolved expression is walked before it is allowed to decide anything.
    """
    if value is None:
        raise MissingRuleFactError(f"missing fact at {where}")
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_absent(item, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_absent(item, f"{where}.{key}")
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
    condition_values = [
        _reject_absent(evaluate_expr(condition, facts), f"applicability.conditions[{index}]")
        for index, condition in enumerate(conditions)
    ]
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
            if condition and _reject_absent(
                evaluate_expr(condition, working_facts), "exceptions.condition"
            ):
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
        condition_matches = bool(_reject_absent(evaluate_expr(condition, working_facts), "rule.if"))
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
