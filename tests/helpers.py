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
