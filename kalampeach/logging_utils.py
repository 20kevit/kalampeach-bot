"""Safe structured logging.

Guarantees:

* secrets known to the application (bot token, webhook secret, NVIDIA key) are
  redacted even if a library or exception message includes them;
* message bodies are never logged by application code — call sites log only
  identifiers, counts, categories and durations.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable, Optional, Sequence

_TOKEN_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),  # Telegram bot token
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{10,}\b"),  # NVIDIA-style API key
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
)
REDACTED = "[REDACTED]"

_LOG_FORMAT = "%(asctime)sZ %(levelname)s %(name)s %(message)s"
_DATEFORMAT = "%Y-%m-%dT%H:%M:%S"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known and pattern-recognizable secrets from a string."""

    if not isinstance(text, str):
        return text
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


class RedactionFilter(logging.Filter):
    """Redact secrets and token patterns from every log record."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        redacted = redact(message, self._secrets)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(
    level: str = "INFO",
    secrets: Sequence[str] = (),
    *,
    stream: Optional[object] = None,
    quiet: bool = False,
) -> logging.Logger:
    """Configure the application logger exactly once per process.

    Logs go to stdout (cPanel cron redirects them to a private file outside
    public_html). No log file is written by the application itself.
    """

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATEFORMAT))
    handler.addFilter(RedactionFilter(secrets))
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Third-party libraries must not spam or leak request URLs (which contain
    # the bot token) at DEBUG level.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger = logging.getLogger("kalampeach")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if quiet:
        logger.setLevel(logging.CRITICAL)
    return logger


def get_logger(name: str = "kalampeach") -> logging.Logger:
    return logging.getLogger(name)
