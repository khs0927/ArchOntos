import json
import logging
import re
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "archontos_http_requests_total", "HTTP requests handled by ArchOntos", ["service", "route"]
)
RULE_EVAL_LATENCY = Histogram(
    "archontos_rule_evaluation_seconds", "Time spent evaluating a canonical rule version"
)

# --- Metrics -------------------------------------------------------------------------------
# Labels are bounded: service (fixed set), route template (or "unmatched"/"unauthorized"),
# HTTP method (normalised below), status class (2xx..5xx) and a closed reason vocabulary.
REQUEST_LATENCY = Histogram(
    "archontos_http_request_duration_seconds",
    "HTTP request latency by route template",
    ["service", "route", "method", "status_class"],
)
REQUESTS_IN_FLIGHT = Gauge(
    "archontos_http_requests_in_flight", "HTTP requests currently being handled", ["service"]
)
AUTH_FAILURES = Counter(
    "archontos_auth_failures_total",
    "Authentication/authorization failures by reason",
    ["service", "reason"],
)

AUTH_REASONS = frozenset(
    {
        "missing_key",
        "invalid_key",
        "invalid_bearer",
        "unauthenticated",
        "forbidden",
        "four_eyes",
        "oidc_rejected",
    }
)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def method_label(method: str) -> str:
    upper = method.upper()
    return upper if upper in _METHODS else "OTHER"


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else "other"


def record_auth_failure(service: str, reason: str) -> None:
    label = reason if reason in AUTH_REASONS else "other"
    AUTH_FAILURES.labels(service=service, reason=label).inc()


# --- Request id ----------------------------------------------------------------------------
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")
_request_id: ContextVar[str | None] = ContextVar("archontos_request_id", default=None)


def sanitize_request_id(raw: str | None) -> str:
    """Accept a caller-supplied X-Request-ID only if it is short and made of safe characters."""
    if raw is not None and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return uuid.uuid4().hex


def set_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


# --- Structured logging --------------------------------------------------------------------
REQUEST_LOGGER = logging.getLogger("archontos.request")
AUDIT_LOGGER = logging.getLogger("archontos.audit")
_HANDLER_MARK = "_archontos_handler"
# Never emitted, whatever a caller passes in.
_FORBIDDEN_FIELDS = frozenset({"authorization", "x-api-key", "api_key", "token", "password"})


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    raw = getattr(record, "fields", None)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if str(k).lower() not in _FORBIDDEN_FIELDS}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = " ".join(f"{k}={v}" for k, v in _fields(record).items())
        return f"{line} {extra}" if extra else line


def configure_logging(log_format: str = "text", log_level: str = "INFO") -> None:
    """Install (idempotently) one stream handler on the ``archontos`` logger."""
    logger = logging.getLogger("archontos")
    for existing in [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]:
        logger.removeHandler(existing)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    setattr(handler, _HANDLER_MARK, True)
    logger.addHandler(handler)
    logger.setLevel(log_level.upper())


def log_request(**fields: Any) -> None:
    REQUEST_LOGGER.info("request", extra={"fields": fields})


def audit_event(
    event: str,
    *,
    outcome: str,
    actor: str,
    permission: str | None = None,
    route: str | None = None,
    **extra: Any,
) -> None:
    """Structured security-decision record. Separate from the DB ``audit_log`` table."""
    fields = {
        "audit": True,
        "event": event,
        "outcome": outcome,
        "actor": actor,
        "permission": permission,
        "route": route,
        "request_id": current_request_id(),
        **extra,
    }
    AUDIT_LOGGER.warning("audit", extra={"fields": fields})
