from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "archontos_http_requests_total", "HTTP requests handled by ArchOntos", ["service", "route"]
)
RULE_EVAL_LATENCY = Histogram(
    "archontos_rule_evaluation_seconds", "Time spent evaluating a canonical rule version"
)


# --- structured request / audit logging ------------------------------------------------------

import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

REQUEST_LOGGER = "archontos.request"
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg plus every ``extra`` field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(fmt: str = "json", level: str = "INFO") -> logging.Logger:
    """Idempotently attach one stdout handler to the request logger."""
    logger = logging.getLogger(REQUEST_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, "_archontos", False):
            logger.removeHandler(handler)
    logger.propagate = False
    if fmt == "off":
        logger.setLevel(logging.CRITICAL + 1)
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler._archontos = True  # type: ignore[attr-defined]
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level.upper() if level.upper() in logging._nameToLevel else "INFO")
    return logger
