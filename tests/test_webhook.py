"""TEST-PLAN B: webhook secret, durable inbox, idempotency, fast acknowledgement."""

from __future__ import annotations

import io
import json
import time
import hmac

import pytest

from kalampeach.db import Database, DatabaseError
from kalampeach.web.app import WebhookApp
from conftest import build_env
from helpers import telegram_update
from kalampeach import repository


def call(
    app,
    *,
    method="POST",
    path="/telegram/webhook",
    body: bytes = b"",
    secret: str = "test_webhook_secret_0123456789",
    content_length=None,
):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SERVER_NAME": "test.local",
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_TYPE": "application/json",
    }
    if content_length is not False:
        environ["CONTENT_LENGTH"] = (
            str(len(body)) if content_length is None else str(content_length)
        )
    if secret is not None:
        environ["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    payload = b"".join(app(environ, start_response))
    return captured, payload


@pytest.fixture
def app(ctx) -> WebhookApp:
    return WebhookApp(ctx.settings, ctx.db)


def inbox_rows(ctx):
    with ctx.db.connection() as connection:
        return connection.execute("SELECT * FROM telegram_updates ORDER BY update_id").fetchall()


def test_valid_update_is_durably_stored(app, ctx, fake_ai):
    payload = telegram_update(update_id=42, message_id=7, text="فروش ۱۰۰۰ تومان")
    captured, body = call(app, body=json.dumps(payload).encode("utf-8"))

    assert captured["status"].startswith("200")
    assert body == b"ok"
    rows = inbox_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["update_id"] == 42
    assert rows[0]["status"] == "queued"
    assert rows[0]["chat_id"] == -1001234567890
    assert fake_ai.calls == [], "the webhook must never call the AI provider"


def test_webhook_acknowledges_quickly(app, ctx, fake_ai):
    payload = telegram_update(update_id=43, message_id=8, text="فروش")
    started = time.perf_counter()
    captured, _ = call(app, body=json.dumps(payload).encode("utf-8"))
    elapsed = time.perf_counter() - started
    assert captured["status"].startswith("200")
    assert elapsed < 0.5
    assert fake_ai.calls == []


def test_missing_secret_is_rejected(app, ctx):
    payload = telegram_update(update_id=44, message_id=9, text="فروش")
    captured, _ = call(app, body=json.dumps(payload).encode("utf-8"), secret=None)
    assert captured["status"].startswith("401")
    assert inbox_rows(ctx) == []


def test_wrong_secret_is_rejected(app, ctx):
    payload = telegram_update(update_id=45, message_id=10, text="فروش")
    captured, _ = call(app, body=json.dumps(payload).encode("utf-8"), secret="wrong-secret")
    assert captured["status"].startswith("401")
    assert inbox_rows(ctx) == []


def test_duplicate_update_id_is_acknowledged_once(app, ctx):
    payload = telegram_update(update_id=46, message_id=11, text="فروش")
    body = json.dumps(payload).encode("utf-8")
    first, _ = call(app, body=body)
    second, _ = call(app, body=body)
    assert first["status"].startswith("200")
    assert second["status"].startswith("200")
    assert len(inbox_rows(ctx)) == 1


def test_malformed_json_is_rejected(app, ctx):
    captured, _ = call(app, body=b"{not json")
    assert captured["status"].startswith("400")
    assert inbox_rows(ctx) == []


def test_missing_update_id_is_rejected(app, ctx):
    captured, _ = call(app, body=json.dumps({"message": {}}).encode("utf-8"))
    assert captured["status"].startswith("400")
    assert inbox_rows(ctx) == []


def test_non_object_payload_is_rejected(app, ctx):
    captured, _ = call(app, body=b"[1, 2, 3]")
    assert captured["status"].startswith("400")


def test_oversized_payload_is_rejected_without_reading(app, ctx):
    captured, _ = call(app, body=b"{}", content_length=ctx.settings.max_webhook_bytes + 10)
    assert captured["status"].startswith("413")
    assert inbox_rows(ctx) == []


def test_missing_content_length_is_rejected(app, ctx):
    captured, _ = call(app, body=b"{}", content_length=False)
    assert captured["status"].startswith("411")


def test_non_post_webhook_is_rejected(app, ctx):
    captured, _ = call(app, method="GET", body=b"")
    assert captured["status"].startswith("405")


def test_unknown_path_returns_404(app):
    captured, _ = call(app, path="/nope", body=b"")
    assert captured["status"].startswith("404")


def test_health_reports_schema_without_secrets(app, ctx):
    captured, body = call(app, method="GET", path="/health", secret=None)
    assert captured["status"].startswith("200")
    document = json.loads(body.decode("utf-8"))
    assert document["status"] == "ok"
    assert document["database_ok"] is True
    assert document["schema_version"] >= 1
    text = body.decode("utf-8")
    assert ctx.settings.telegram_bot_token not in text
    assert ctx.settings.nvidia_api_key not in text


def test_health_is_degraded_when_database_is_unavailable(tmp_path, ctx):
    broken = Database(tmp_path / "not_a_dir" / "bot.sqlite3", create_dirs=False)
    app = WebhookApp(ctx.settings, broken)
    captured, body = call(app, method="GET", path="/health", secret=None)
    assert captured["status"].startswith("503")
    assert json.loads(body.decode("utf-8"))["database_ok"] is False


def test_database_failure_returns_503_so_telegram_retries(app, ctx, monkeypatch):
    payload = telegram_update(update_id=47, message_id=12, text="فروش")

    def boom(*args, **kwargs):
        raise DatabaseError("disk full")

    monkeypatch.setattr(ctx.db, "transaction", boom)
    captured, _ = call(app, body=json.dumps(payload).encode("utf-8"))
    assert captured["status"].startswith("503")


def test_duplicate_delivery_does_not_create_second_inbox_row(app, ctx):
    payload = telegram_update(update_id=48, message_id=13, text="فروش")
    body = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        captured, _ = call(app, body=body)
        assert captured["status"].startswith("200")
    rows = inbox_rows(ctx)
    assert len(rows) == 1


def test_webhook_stores_raw_payload_for_audit(app, ctx):
    payload = telegram_update(update_id=49, message_id=14, text="فروش ۱۲۰۰ تومان")
    call(app, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    row = inbox_rows(ctx)[0]
    assert "فروش" in row["raw_payload"]
    assert json.loads(row["raw_payload"])["update_id"] == 49
