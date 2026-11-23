"""Webhook secret validation, durable inbox and fast acknowledgement (B)."""

from __future__ import annotations

import json

from app.repo import inbox
from app.timeutil import to_utc_iso
from tests.helpers import FIXED_NOW, WEBHOOK_SECRET, group_update


def post(client, payload, secret=WEBHOOK_SECRET):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return client.post("/telegram/webhook", data=json.dumps(payload), headers=headers)


def _inbox_rows(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM telegram_inbox").fetchall()]


def test_valid_secret_is_accepted_and_update_is_persisted(client, ctx):
    response = post(client, group_update(1, "سلام"))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    rows = _inbox_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["update_id"] == 1
    assert rows[0]["status"] == "queued"


def test_missing_secret_is_rejected_without_storing(client, ctx):
    response = post(client, group_update(1, "سلام"), secret=None)
    assert response.status_code == 403
    assert _inbox_rows(ctx) == []


def test_wrong_secret_is_rejected_without_storing(client, ctx):
    response = post(client, group_update(1, "سلام"), secret="wrong-secret-value-000000")
    assert response.status_code == 403
    assert _inbox_rows(ctx) == []
    assert ctx.ai.extraction_requests == []


def test_duplicate_update_id_is_acknowledged_once(client, ctx):
    payload = group_update(7, "سلام")
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200
    rows = _inbox_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["update_id"] == 7


def test_webhook_never_calls_ai_and_returns_promptly(client, ctx):
    response = post(client, group_update(3, "فروش ۵۰۰ هزار تومان"))
    assert response.status_code == 200
    assert ctx.ai.extraction_requests == []
    assert ctx.telegram.sent == []
    # No business rows are created in the request path.
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_invalid_json_is_rejected(client, ctx):
    response = client.post(
        "/telegram/webhook",
        data="{not json",
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    assert response.status_code == 400
    assert _inbox_rows(ctx) == []


def test_update_without_update_id_is_rejected(client, ctx):
    response = post(client, {"message": {"message_id": 1}})
    assert response.status_code == 400
    assert _inbox_rows(ctx) == []


def test_oversized_update_is_rejected(client, ctx):
    big = group_update(9, "x" * 2_000_000)
    response = post(client, big)
    assert response.status_code == 413
    assert _inbox_rows(ctx) == []


def test_healthz_reports_schema_and_timezone(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["schema_version"] >= 1
    assert body["timezone"] == "Asia/Tehran"


def test_webhook_returns_503_when_schema_is_not_initialised(tmp_path, fake_ai, fake_telegram):
    from app.context import AppContext
    from app.db import Database
    from app.wsgi import create_app
    from tests.helpers import make_config

    config = make_config(tmp_path)
    ctx = AppContext(
        config=config,
        db=Database(config.database_path),
        ai=fake_ai,
        telegram=fake_telegram,
    )
    app = create_app(ctx)
    client = app.test_client()
    response = post(client, group_update(1, "سلام"))
    assert response.status_code == 503
    assert client.get("/healthz").status_code == 503


def test_unknown_route_returns_json_404(client):
    response = client.get("/")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False}


def test_worker_marks_inbox_done_after_processing(client, ctx):
    post(client, group_update(11, "سلام"))
    ctx.ai.queue_extraction(
        {
            "classification": "non_business",
            "event_type": None,
            "confidence": 0.9,
            "event_date": None,
            "amount": None,
            "currency": None,
            "item_name": None,
            "quantity": None,
            "unit": None,
            "inventory_direction": None,
            "description": None,
            "missing_fields": [],
            "ambiguities": [],
            "reason": None,
        }
    )
    from app.services.worker import run_worker

    with ctx.db.session() as conn:
        stats = run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    assert stats.ignored == 1
    rows = _inbox_rows(ctx)
    assert rows[0]["status"] == "ignored"
    assert rows[0]["processed_at_utc"] is not None


def test_enqueue_is_idempotent_at_repository_level(ctx):
    with ctx.db.session() as conn:
        now = to_utc_iso(FIXED_NOW)
        inserted_a, id_a = inbox.enqueue(
            conn, update_id=42, payload_json="{}", kind="message", now_iso=now
        )
        inserted_b, id_b = inbox.enqueue(
            conn, update_id=42, payload_json="{}", kind="message", now_iso=now
        )
    assert inserted_a is True
    assert inserted_b is False
    assert id_a == id_b
