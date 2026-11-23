"""Secret-safe structured logging.

Logs are single-line JSON objects written to stderr (captured by cPanel) or to
an absolute log path. Rules enforced here and by reviewers:

* never log message text, bot tokens, API keys or full Telegram payloads;
* never log AI prompts or raw AI responses;
* log stable error categories instead of raw exception text where a provider
  message could echo request data.

:func:`redact` is a final safety net for messages that must include an
exception string.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

LOGGER_NAME = "kalampeach"
_REDACTED = "[REDACTED]"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({key: _safe_value(value) for key, value in extra.items()})
        if record.exc_info:
            # Only the exception type and its message; the message must already
            # be safe (categories, key names, file paths).
            exc_type = record.exc_info[0]
            payload["exception"] = exc_type.__name__ if exc_type else "Exception"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return str(type(value).__name__)


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure the package logger exactly once per process."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if log_file:
        parent = os.path.dirname(log_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


def log_event(
    logger: logging.Logger, level: int, event: str, **fields: Any
) -> None:
    """Log an event with structured fields."""

    logger.log(level, event, extra={"fields": fields})


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Remove known secret values from a string before logging it."""

    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, _REDACTED)
    return result
