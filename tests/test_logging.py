"""Secret-safe, message-free structured logging (SECURITY.md, TEST-PLAN G)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.logging_setup import JsonFormatter, configure_logging, get_logger, redact

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = sorted((ROOT / "app").rglob("*.py"))


def test_log_lines_are_json_and_secret_free(tmp_path):
    log_file = tmp_path / "logs" / "bot.log"
    configure_logging("INFO", str(log_file))
    logger = get_logger("test")
    logger.info("event_saved", extra={"fields": {"event_id": 3, "event_type": "sale_revenue"}})

    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "event_saved"
    assert payload["event_id"] == 3
    assert payload["level"] == "INFO"
    assert "nvapi" not in line
    assert "TELEGRAM_BOT_TOKEN" not in line


def test_exception_logging_records_only_the_type(tmp_path):
    log_file = tmp_path / "bot.log"
    logger = logging.getLogger("kalampeach.exception-test")
    logger.handlers = []
    logger.propagate = False
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        raise ValueError("secret-looking payload 123456:AAbbCC")
    except ValueError:
        logger.error("update_processing_failed", exc_info=True)
    handler.close()

    payload = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert payload["exception"] == "ValueError"
    assert "123456:AAbbCC" not in log_file.read_text(encoding="utf-8")


def test_redact_removes_configured_secrets():
    message = "failed with token 123456:SECRET and key nvapi-SECRET"
    cleaned = redact(message, ["123456:SECRET", "nvapi-SECRET", None])
    assert "SECRET" not in cleaned
    assert cleaned.count("[REDACTED]") == 2


def test_application_logging_never_includes_message_text_or_payloads():
    forbidden = ('"message_text"', '"payload_json"', "message.text", "correction_text", "answer_text")
    for path in APP_FILES:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            for match in re.finditer(re.escape(pattern), text):
                # Only logging context matters: inspect the surrounding 200 chars.
                window = text[max(0, match.start() - 200) : match.end() + 200]
                assert "logger." not in window or "extra=" not in window, (
                    f"{pattern!r} appears next to a log call in {path}"
                )
