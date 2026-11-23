"""WSGI application: the Telegram webhook endpoint.

Design constraints from the hosting target:

* runs under cPanel's Python/Passenger WSGI facility (``passenger_wsgi.py``
  exposes ``application``);
* the webhook request validates Telegram's secret token, durably enqueues the
  update, and returns promptly — no AI call, no report work, no long loop;
* a duplicate ``update_id`` is acknowledged, never duplicated;
* an uninitialised or outdated schema returns 503 instead of touching data.

The heavy lifting happens in the cron worker (``scripts/run_worker.py``).
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
from typing import Any, Callable, Mapping

from flask import Flask, Response, jsonify, request

from app.context import AppContext
from app.db import require_current_schema, schema_version
from app.errors import ConfigError, SchemaError
from app.logging_setup import configure_logging, get_logger
from app.repo import inbox as inbox_repo
from app.timeutil import to_utc_iso, utc_now

WEBHOOK_PATH = "/telegram/webhook"
MAX_UPDATE_BYTES = 1_000_000

logger = get_logger("webhook")


def create_app(ctx: AppContext) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPDATE_BYTES
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.before_request
    def _start_timer() -> None:
        request.environ["app.request_started"] = time.monotonic()

    @app.after_request
    def _log_request(response: Response) -> Response:
        started = request.environ.get("app.request_started")
        duration_ms = int((time.monotonic() - started) * 1000) if started else -1
        logger.info(
            "http_request",
            extra={
                "fields": {
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                }
            },
        )
        return response

    @app.post(WEBHOOK_PATH)
    def telegram_webhook() -> Response:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not _valid_secret(provided, ctx.config.telegram_webhook_secret):
            logger.warning("webhook_secret_rejected", extra={"fields": {"path": request.path}})
            return jsonify({"ok": False}), 403

        raw = request.get_data(cache=False)
        if len(raw) > MAX_UPDATE_BYTES:
            return jsonify({"ok": False}), 413
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return jsonify({"ok": False}), 400
        if not isinstance(payload, Mapping):
            return jsonify({"ok": False}), 400

        update_id = payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            return jsonify({"ok": False}), 400

        now_iso = to_utc_iso(ctx.now())
        try:
            with ctx.db.session() as conn:
                require_current_schema(conn)
                inserted, _row_id = inbox_repo.enqueue(
                    conn,
                    update_id=update_id,
                    payload_json=json.dumps(dict(payload), ensure_ascii=False),
                    kind=_kind_of(payload),
                    now_iso=now_iso,
                )
        except SchemaError:
            logger.error("webhook_schema_not_ready", extra={"fields": {"action": "run scripts/init_db.py"}})
            return jsonify({"ok": False}), 503
        except sqlite3.Error:
            # Telegram retries non-2xx responses; nothing was acknowledged.
            logger.error("webhook_store_failed", extra={"fields": {"store": "inbox"}})
            return jsonify({"ok": False}), 503

        logger.info(
            "update_received",
            extra={"fields": {"update_id": update_id, "duplicate": not inserted}},
        )
        # Acknowledge only after durable persistence (or after confirming the
        # update is already stored).
        return jsonify({"ok": True})

    @app.get("/healthz")
    def healthz() -> Response:
        try:
            with ctx.db.session() as conn:
                version = schema_version(conn)
                ready = version > 0
        except SchemaError:
            return jsonify({"status": "database_not_ready"}), 503
        except sqlite3.Error:
            return jsonify({"status": "database_unavailable"}), 503
        if not ready:
            return jsonify({"status": "schema_not_initialised"}), 503
        return jsonify(
            {
                "status": "ok",
                "schema_version": version,
                "timezone": ctx.config.business_timezone,
                "daily_report_time": ctx.config.daily_report_time,
            }
        )

    @app.errorhandler(413)
    def _too_large(_error: Any) -> Response:
        return jsonify({"ok": False}), 413

    @app.errorhandler(404)
    def _not_found(_error: Any) -> Response:
        return jsonify({"ok": False}), 404

    @app.errorhandler(405)
    def _not_allowed(_error: Any) -> Response:
        return jsonify({"ok": False}), 405

    @app.errorhandler(Exception)
    def _unhandled(error: Exception) -> Response:
        logger.error(
            "http_unhandled_error", extra={"fields": {"exception": type(error).__name__}}
        )
        return jsonify({"ok": False}), 500

    return app


def _valid_secret(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _kind_of(payload: Mapping[str, Any]) -> str | None:
    if "message" in payload:
        return "message"
    if "edited_message" in payload:
        return "edited_message"
    if "channel_post" in payload or "edited_channel_post" in payload:
        return "channel_post"
    if "callback_query" in payload:
        return "callback_query"
    return "other"


class ConfigErrorApp:
    """Minimal WSGI app served while configuration is incomplete.

    Returning 503 (rather than an opaque crash) keeps the endpoint reachable for
    the operator while the log names the missing keys. No secret or value is
    ever exposed over HTTP or in the log.
    """

    def __init__(self, problems: list[str]) -> None:
        self._problems = problems
        configure_logging("ERROR")
        logger.critical(
            "configuration_invalid",
            extra={"fields": {"problem_count": len(problems), "keys": problems}},
        )

    def __call__(self, environ: dict, start_response: Callable[..., Any]) -> list[bytes]:
        import json as _json

        body = _json.dumps(
            {"ok": False, "status": "configuration_incomplete"}, separators=(",", ":")
        ).encode("utf-8")
        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]


def build_application(environ: Mapping[str, str] | None = None) -> Any:
    """Build the WSGI callable from the process environment."""

    from app.bootstrap import build_context

    try:
        ctx = build_context(environ=environ)
    except ConfigError as exc:
        return ConfigErrorApp(exc.problems)
    return create_app(ctx)


def _utc_now_iso() -> str:
    return to_utc_iso(utc_now())


application = build_application()
