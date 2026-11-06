"""Structured logging with secret redaction.

Use ``logger.info("event.name", extra={"fields": {...}})`` to attach structured fields.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]+)")
REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """Recursively redact secret-looking keys and values."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub(REDACTED, value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact(fields))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name}: {redact(record.getMessage())}"
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            base += " " + json.dumps(redact(fields), default=str)
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger("relay")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"relay.{name}")
