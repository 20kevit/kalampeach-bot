"""Minimal, dependency-free WSGI application for cPanel/Passenger.

Routes
------
``POST /telegram/webhook``
    Validates ``X-Telegram-Bot-Api-Secret-Token``, durably enqueues the update by
    ``update_id``, and returns immediately. **No AI work and no Telegram calls
    happen here**, so Telegram's webhook timeout is never at risk.
``GET /health``
    Safe diagnostics for operators (no secrets, no message content).
Everything else is 404.

The callable is intentionally framework-free: one route, no templates, no
sessions, and therefore no third-party dependency to install on shared hosting.
"""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
import time
from typing import Callable, Iterable, List, Optional, Tuple

from .. import __version__, migrations, repository
from ..config import WEBHOOK_PATH, ConfigError, Settings, load_settings
from ..db import Database, DatabaseError
from ..logging_utils import configure_logging
from ..telegram.updates import extract_chat_hint
from ..timeutil import utc_now, to_utc_iso

logger = logging.getLogger("kalampeach.web")

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


class WebhookApp:
    def __init__(self, settings: Settings, db: Optional[Database] = None) -> None:
        self.settings = settings
        self.db = db or Database(
            settings.database_path,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
            journal_mode=settings.sqlite_journal_mode,
        )

    # -- WSGI entry point --------------------------------------------------
    def __call__(self, environ, start_response) -> Iterable[bytes]:
        started = time.monotonic()
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = _path(environ)
        status_holder = {}

        def _start(status, headers):
            status_holder["status"] = status
            start_response(status, headers)

        if path == self.settings.webhook_path:
            if method != "POST":
                return _respond(_start, "405 Method Not Allowed", b"method not allowed\n", headers=[("Allow", "POST")])
            body = self._handle_webhook(environ, _start)
        elif path == "/health":
            if method not in ("GET", "HEAD"):
                return _respond(_start, "405 Method Not Allowed", b"method not allowed\n", headers=[("Allow", "GET, HEAD")])
            body = self._handle_health(_start)
        else:
            body = _respond(_start, "404 Not Found", b"not found\n")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "web_request method=%s path=%s status=%s duration_ms=%d",
            method,
            path,
            status_holder.get("status", "-").split(" ")[0],
            elapsed_ms,
        )
        return body

    # -- webhook -----------------------------------------------------------
    def _handle_webhook(self, environ, start_response) -> List[bytes]:
        if not _secret_matches(environ, self.settings.webhook_secret):
            logger.warning("webhook_rejected reason=bad_secret")
            return _respond(start_response, "401 Unauthorized", b"unauthorized\n")

        length = _content_length(environ)
        if length is None:
            logger.warning("webhook_rejected reason=missing_length")
            return _respond(start_response, "411 Length Required", b"length required\n")
        if length > self.settings.max_webhook_bytes:
            logger.warning("webhook_rejected reason=payload_too_large bytes=%d", length)
            return _respond(start_response, "413 Payload Too Large", b"payload too large\n")

        raw = _read_body(environ, length)
        if len(raw) != length:
            logger.warning("webhook_rejected reason=short_body")
            return _respond(start_response, "400 Bad Request", b"bad request\n")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("webhook_rejected reason=invalid_json")
            return _respond(start_response, "400 Bad Request", b"bad request\n")
        if not isinstance(payload, dict):
            logger.warning("webhook_rejected reason=not_an_object")
            return _respond(start_response, "400 Bad Request", b"bad request\n")

        update_id = payload.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            logger.warning("webhook_rejected reason=missing_update_id")
            return _respond(start_response, "400 Bad Request", b"bad request\n")

        chat_hint = extract_chat_hint(payload)
        now_iso = to_utc_iso(utc_now())
        try:
            with self.db.transaction() as connection:
                created = repository.enqueue_update(
                    connection,
                    update_id=int(update_id),
                    raw_payload=raw.decode("utf-8"),
                    chat_id=chat_hint["chat_id"],
                    chat_type=chat_hint["chat_type"],
                    now=now_iso,
                )
        except (DatabaseError, sqlite3.Error) as exc:
            # Telegram retries non-2xx responses; nothing was lost.
            logger.error("webhook_enqueue_failed error=%s", exc.__class__.__name__)
            return _respond(start_response, "503 Service Unavailable", b"database unavailable\n")

        if created:
            logger.info("webhook_enqueued update_id=%s chat_type=%s", update_id, chat_hint["chat_type"])
        else:
            logger.info("webhook_duplicate update_id=%s", update_id)
        return _respond(start_response, "200 OK", b"ok")

    # -- health ------------------------------------------------------------
    def _handle_health(self, start_response) -> List[bytes]:
        health = self.db.health()
        try:
            expected = migrations.latest_version()
        except migrations.MigrationError:  # pragma: no cover - packaging problem
            expected = None
        schema_version = health.get("schema_version")
        # The web process never migrates; it reports degraded until an operator
        # (or the cron worker) has applied the shipped schema version.
        ready = bool(health.get("ok")) and expected is not None and schema_version == expected
        payload = {
            "status": "ok" if ready else "degraded",
            "version": __version__,
            "database_ok": bool(health.get("ok")),
            "schema_version": schema_version,
            "expected_schema_version": expected,
            "webhook_path": self.settings.webhook_path,
            "review_needed": health.get("review_needed", {}),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status = "200 OK" if ready else "503 Service Unavailable"
        return _respond(start_response, status, body, content_type=JSON_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _path(environ) -> str:
    path = environ.get("PATH_INFO") or "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _secret_matches(environ, expected: str) -> bool:
    supplied = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN")
    if not isinstance(supplied, str) or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _content_length(environ) -> Optional[int]:
    raw = environ.get("CONTENT_LENGTH")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _read_body(environ, length: int) -> bytes:
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    try:
        return stream.read(length)
    except Exception:  # pragma: no cover - defensive
        return b""


def _respond(
    start_response: Callable,
    status: str,
    body: bytes = b"",
    *,
    content_type: str = TEXT_CONTENT_TYPE,
    headers: Optional[List[Tuple[str, str]]] = None,
) -> List[bytes]:
    response_headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    if headers:
        response_headers.extend(headers)
    start_response(status, response_headers)
    return [body]


# ---------------------------------------------------------------------------
# Passenger / lazy application factory
# ---------------------------------------------------------------------------

_APP = None
_APP_ERROR: Optional[ConfigError] = None


def create_app(settings: Settings, db: Optional[Database] = None) -> WebhookApp:
    return WebhookApp(settings, db)


def _bootstrap_error_response(problems: Tuple[str, ...]):
    logger.critical("configuration_invalid problems=%s", "; ".join(problems))

    def app(environ, start_response):
        body = (
            "KalamPeach bot is not configured.\n"
            "Run `python scripts/doctor.py` on the server for details.\n"
        ).encode("utf-8")
        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", TEXT_CONTENT_TYPE),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]

    return app


def application(environ, start_response):
    """WSGI entry point used by Passenger (see ``passenger_wsgi.py``)."""

    global _APP, _APP_ERROR
    if _APP is None and _APP_ERROR is None:
        try:
            settings = load_settings()
            configure_logging(settings.log_level, settings.redaction_secrets())
            _APP = create_app(settings)
            logger.info("app_started version=%s", __version__)
        except ConfigError as exc:
            _APP_ERROR = exc
            _APP = _bootstrap_error_response(exc.problems)
        except Exception as exc:  # pragma: no cover - defensive
            logging.basicConfig(level=logging.ERROR)
            logger.critical("app_start_failed error=%s", exc.__class__.__name__)
            _APP = _bootstrap_error_response(("startup failed",))
    return _APP(environ, start_response)
