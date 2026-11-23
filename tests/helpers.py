"""Test helpers: Telegram payload builders and worker drivers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from kalampeach import repository
from kalampeach.timeutil import to_utc_iso
from kalampeach.services.worker import Worker

from conftest import EMPLOYEE_ID, GROUP_ID, MANAGER_ID


def telegram_update(
    *,
    update_id: int,
    message_id: int,
    text: Optional[str] = None,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
    sender_id: Optional[int] = EMPLOYEE_ID,
    reply_to: Optional[int] = None,
    when: datetime = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc),
    media: Optional[str] = None,
    is_bot: bool = False,
    first_name: str = "کارمند",
) -> dict:
    message = {
        "message_id": message_id,
        "date": int(when.timestamp()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {
            "id": sender_id,
            "is_bot": is_bot,
            "first_name": first_name,
            "username": "staff_user",
        },
    }
    if text is not None:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    if media is not None:
        message[media] = [{"file_id": "abc"}] if media == "photo" else {"file_id": "abc"}
    return {"update_id": update_id, "message": message}


def private_update(*, update_id: int, message_id: int, text: str, **kwargs) -> dict:
    kwargs.setdefault("chat_id", EMPLOYEE_ID)
    kwargs.setdefault("chat_type", "private")
    kwargs.setdefault("sender_id", EMPLOYEE_ID)
    return telegram_update(update_id=update_id, message_id=message_id, text=text, **kwargs)


def manager_update(*, update_id: int, message_id: int, text: str, **kwargs) -> dict:
    kwargs.setdefault("chat_id", MANAGER_ID)
    kwargs.setdefault("chat_type", "private")
    kwargs.setdefault("sender_id", MANAGER_ID)
    return telegram_update(update_id=update_id, message_id=message_id, text=text, **kwargs)


def enqueue(ctx, payload: dict) -> bool:
    with ctx.db.transaction() as connection:
        return repository.enqueue_update(
            connection,
            update_id=int(payload["update_id"]),
            raw_payload=json.dumps(payload, ensure_ascii=False),
            chat_id=payload["message"]["chat"]["id"] if "message" in payload else None,
            chat_type=None,
            now=to_utc_iso(ctx.now()),
        )


def run_worker(ctx, *, now=None, skip_reports: bool = False, worker: Optional[Worker] = None):
    worker = worker or Worker(ctx)
    return worker.run_once(now=now or ctx.now(), skip_reports=skip_reports)


def process(ctx, payload: dict, **kwargs):
    enqueue(ctx, payload)
    return run_worker(ctx, **kwargs)


def fetch_all(ctx, sql: str, params=()):
    with ctx.db.connection() as connection:
        return connection.execute(sql, params).fetchall()


def fetch_one(ctx, sql: str, params=()):
    with ctx.db.connection() as connection:
        return connection.execute(sql, params).fetchone()


def events(ctx):
    return fetch_all(ctx, "SELECT * FROM events ORDER BY id")


def revisions(ctx, event_id: int):
    return fetch_all(
        ctx, "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revision_number", (event_id,)
    )


def clarifications(ctx):
    return fetch_all(ctx, "SELECT * FROM clarifications ORDER BY id")


def source_messages(ctx):
    return fetch_all(ctx, "SELECT * FROM source_messages ORDER BY id")


def report_deliveries(ctx):
    return fetch_all(ctx, "SELECT * FROM report_deliveries ORDER BY id")


def bot_messages(ctx, kind: Optional[str] = None):
    if kind:
        return fetch_all(ctx, "SELECT * FROM bot_messages WHERE kind = ? ORDER BY message_id", (kind,))
    return fetch_all(ctx, "SELECT * FROM bot_messages ORDER BY message_id")


def seed_pending_clarification(
    ctx,
    *,
    seq: int = 1,
    employee_id: int = EMPLOYEE_ID,
    question: str = "مبلغ این خرید چقدر بود؟",
    missing=("amount",),
    status: str = "pending_delivery",
    lease_expires_at: Optional[str] = None,
    chunk_hint: str = "خرید",
):
    """Create a pending event plus its clarification, as capture would leave it.

    Returns ``(event_id, clarification_id)``. Used to exercise delivery in
    isolation from the capture path.
    """

    now = to_utc_iso(ctx.now())
    with ctx.db.transaction() as connection:
        repository.upsert_user(
            connection,
            telegram_user_id=employee_id,
            display_name="کارمند نمونه",
            role="employee",
            now=now,
        )
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=500000 + seq,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=600000 + seq,
            reply_to_message_id=None,
            sender_telegram_user_id=employee_id,
            message_text="seed pending %d" % seq,
            message_timestamp_utc=now,
            processing_status="pending",
            now=now,
        )
        event_id = repository.insert_event(
            connection,
            event_type="expense_purchase",
            status="pending_clarification",
            event_date="2026-11-23",
            amount=None,
            currency="تومان",
            currency_source="explicit",
            item_name=None,
            quantity=None,
            unit=None,
            inventory_direction=None,
            description=chunk_hint,
            created_by_telegram_user_id=employee_id,
            source_message_id=int(source["id"]),
            status_reason="missing_fields:" + ",".join(missing),
            now=now,
        )
        repository.insert_revision(
            connection,
            event_id=int(event_id),
            revision_number=1,
            actor_telegram_user_id=employee_id,
            change_type="created",
            change_reason="pending_clarification",
            source_message_id=int(source["id"]),
            clarification_id=None,
            previous_snapshot=None,
            new_snapshot="{}",
            validation_status="pending",
            now=now,
        )
        clarification_id = repository.insert_clarification(
            connection,
            event_id=int(event_id),
            employee_telegram_user_id=employee_id,
            question_text=question,
            missing_fields=list(missing),
            now=now,
        )
        if status != "pending_delivery" or lease_expires_at is not None:
            connection.execute(
                "UPDATE clarifications SET status = ?, lease_owner = ?, "
                "lease_expires_at_utc = ? WHERE id = ?",
                (
                    status,
                    "dead-worker" if lease_expires_at else None,
                    lease_expires_at,
                    int(clarification_id),
                ),
            )
    return int(event_id), int(clarification_id)


def with_worker(ctx, worker_id: str):
    """A second application context sharing state but with its own worker id."""

    from dataclasses import replace

    return replace(ctx, worker_id=worker_id)


def seed_report_delivery(
    ctx,
    *,
    report_key: str = "daily:9999:2026-11-23",
    status: str = "pending",
    chunks: int = 2,
    chunks_sent: int = 0,
    attempts: int = 1,
    snapshot: Optional[bool] = True,
    lease_expires_at: Optional[str] = None,
    text: Optional[str] = None,
    manager_id: int = MANAGER_ID,
):
    """Insert a report delivery row in an arbitrary delivery state."""

    body = text or ("📊 گزارش روزانه\n\n" + ("خط گزارش\n\n" * 1200))
    now = to_utc_iso(ctx.now())
    snapshot_json = (
        json.dumps({"facts": {}, "text": body, "narrative_used": False}, ensure_ascii=False)
        if snapshot
        else None
    )
    with ctx.db.transaction() as connection:
        connection.execute(
            "INSERT INTO report_deliveries (report_key, report_type, variant, "
            "manager_telegram_user_id, period_start_date, period_end_date, scheduled_at_utc, "
            "status, attempts, chunk_count, chunks_sent, telegram_message_ids, snapshot_json, "
            "lease_owner, lease_expires_at_utc, created_at_utc, updated_at_utc) "
            "VALUES (?, 'daily', 'daily', ?, '2026-11-23', '2026-11-23', "
            "'2026-11-23T20:00:00Z', ?, ?, ?, ?, '[111]', ?, ?, ?, ?, ?)",
            (
                report_key,
                int(manager_id),
                status,
                int(attempts),
                int(chunks),
                int(chunks_sent),
                snapshot_json,
                "dead-worker" if lease_expires_at else None,
                lease_expires_at,
                now,
                now,
            ),
        )
    return int(
        fetch_one(ctx, "SELECT id FROM report_deliveries WHERE report_key = ?", (report_key,))["id"]
    )


def seed_event(
    ctx,
    seq: int,
    *,
    event_type: str = "sale_revenue",
    amount: Optional[str] = "1000",
    currency: Optional[str] = "تومان",
    event_date: str = "2026-11-23",
    status: str = "confirmed",
    item_name: Optional[str] = None,
    quantity: Optional[str] = None,
    unit: Optional[str] = None,
    direction: Optional[str] = None,
    description: Optional[str] = None,
    sender_id: int = EMPLOYEE_ID,
):
    """Insert a confirmed/pending event straight into the database."""

    now = to_utc_iso(ctx.now())
    with ctx.db.transaction() as connection:
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=100000 + seq,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=200000 + seq,
            reply_to_message_id=None,
            sender_telegram_user_id=sender_id,
            message_text="seed %d" % seq,
            message_timestamp_utc=now,
            processing_status="processed",
            now=now,
        )
        repository.upsert_user(
            connection,
            telegram_user_id=sender_id,
            display_name="کارمند نمونه",
            role="employee",
            now=now,
        )
        return repository.insert_event(
            connection,
            event_type=event_type,
            status=status,
            event_date=event_date,
            amount=amount,
            currency=currency,
            currency_source="explicit" if currency else None,
            item_name=item_name,
            quantity=quantity,
            unit=unit,
            inventory_direction=direction,
            description=description,
            created_by_telegram_user_id=sender_id,
            source_message_id=int(source["id"]),
            status_reason=None,
            now=now,
        )


__all__ = [
    "seed_event",
    "seed_pending_clarification",
    "seed_report_delivery",
    "with_worker",
    "telegram_update",
    "private_update",
    "manager_update",
    "enqueue",
    "run_worker",
    "process",
    "fetch_all",
    "fetch_one",
    "events",
    "revisions",
    "clarifications",
    "source_messages",
    "report_deliveries",
    "bot_messages",
    "rebuild_ctx",
    "EMPLOYEE_ID",
    "MANAGER_ID",
    "GROUP_ID",
]


def rebuild_ctx(ctx, **settings_overrides):
    """Return a new AppContext sharing the same DB/AI/Telegram/clock but with
    overridden settings (e.g. DEFAULT_CURRENCY, allowlists)."""

    from dataclasses import replace

    from kalampeach.services.context import AppContext

    settings = replace(ctx.settings, **settings_overrides)
    return AppContext(
        settings=settings,
        db=ctx.db,
        ai=ctx.ai,
        telegram=ctx.telegram,
        clock=ctx.clock,
        logger=ctx.logger,
        worker_id=ctx.worker_id,
    )
