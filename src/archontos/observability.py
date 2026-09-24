from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "archontos_http_requests_total", "HTTP requests handled by ArchOntos", ["service", "route"]
)
RULE_EVAL_LATENCY = Histogram(
    "archontos_rule_evaluation_seconds", "Time spent evaluating a canonical rule version"
)
