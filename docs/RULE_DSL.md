# OAIF rule DSL v1

The first evaluator uses a constrained JSON expression language. It is intentionally smaller than arbitrary Python and is safe to persist as data.

```json
{
  "applicability": {
    "jurisdiction": ["KR", "KR-11"],
    "building_use_groups": ["공동주택"],
    "conditions": [
      {">=": [{"var": "building.floor_count"}, 7]}
    ]
  },
  "rule": {
    "if": {">=": [{"var": "stair.direct_count"}, 2]},
    "then": {"PASS": {"reason": "minimum satisfied"}},
    "else": {
      "FAIL": {
        "reason": "직통계단 부족",
        "required": 2,
        "actual": {"var": "stair.direct_count"}
      }
    }
  },
  "exceptions": [],
  "delegation": {}
}
```

Supported expression operators in the first implementation:

- `var`
- `and` / `all`
- `or` / `any`
- `not`
- `==`, `!=`, `>=`, `<=`, `>`, `<`
- `in`

Unknown operators fail closed with `RuleEvaluationError`; they are never silently interpreted.

## Missing facts

The project's governing rule is that an unevaluable rule yields `REVIEW`, never
`PASS` and never `FAIL`. A `var` that is not present in the facts resolves to
`None`, and any resolved expression that still contains a `None` anywhere in it,
at any nesting depth, raises `MissingRuleFactError`, which the caller turns into
a `REVIEW` outcome with the reason `Insufficient facts to evaluate rule`.

This applies to a bare `var` used as the whole condition, to a var nested in a
list or object literal, and to every operator operand. It is deliberately not
a top-level-only check: `bool(None)` is `False` and `bool([None])` is `True`, so
a shallow check let an absent fact produce a definitive `FAIL` in the first case
and a definitive `PASS` in the second.

Two consequences a rule author must know:

- A `None` literal is not expressible as a rule operand. Writing a comparison
  against `None`, such as a literal `None` as the right-hand side of an
  equality, raises `MissingRuleFactError` rather than comparing. Model absence
  with an absent `var`, not with a null literal.
- The default form `var` with a fallback, written as a two-element list where
  the first element is the path and the second is the default, substitutes the
  default when the fact is absent. A non-`None` default therefore re-opens the
  fail-open that the guards close: an absent fact resolves to the default and
  the rule decides on it. Prefer an absent `var` so the rule goes to `REVIEW`.
  A `None` default is still routed to `REVIEW` by the same guard.

## Scope

An `applicability` block that is present but resolves to no usable scope clause
is a separate question from a missing fact, and its behaviour is described where
the scope resolution lives, not here. A wholly absent `applicability` means the
rule is unscoped.
