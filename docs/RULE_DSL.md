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
