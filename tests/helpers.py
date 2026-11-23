"""Test doubles and factories. No network, no credentials, no real clock."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from app.ai.schema import (
    Extraction,
    ManagerIntent,
    validate_correction,
    validate_extraction,
)
from app.config import load_config
from app.context import AppContext
from app.db import Database
from app.errors import AIInvalidOutput, AIUnavailable
from app.timeutil import to_utc_iso

GROUP_ID = -1001234567890
MANAGER_ID = 900001
EMPLOYEE_ID = 900002
OTHER_EMPLOYEE_ID = 900003
OUTSIDER_ID = 900999
WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef"

FIXED_NOW = datetime(2025, 12, 23, 10, 0, tzinfo=timezone.utc)  # 13:30 Tehran
TEHRAN_TODAY = date(2025, 12, 23)

BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN-NOT-REAL",
    "TELEGRAM_GROUP_ID": str(GROUP_ID),
    "MANAGER_TELEGRAM_ID": str(MANAGER_ID),
    "EMPLOYEE_TELEGRAM_IDS": f"{EMPLOYEE_ID},{OTHER_EMPLOYEE_ID}",
    "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "WEBHOOK_PUBLIC_URL": "https://bot.example.test/telegram/webhook",
    "TELEGRAM_API_BASE_URL": "https://api.telegram.org",
    "NVIDIA_API_KEY": "nvapi-TEST-NOT-REAL",
    "NVIDIA_BASE_URL": "https://nvidia.example.test/v1",
    "NVIDIA_MODEL": "owner-supplied-model",
    "BUSINESS_TIMEZONE": "Asia/Tehran",
    "DAILY_REPORT_TIME": "23:30",
    "DATABASE_PATH": "/tmp/kalampeach-test/bot.sqlite3",
    "LOG_LEVEL": "CRITICAL",
}


def env_for(tmp_path, **overrides: str | None) -> dict[str, str]:
    env = dict(BASE_ENV)
    env["DATABASE_PATH"] = str(tmp_path / "data" / "bot.sqlite3")
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def make_config(tmp_path, **overrides: str | None):
    from pathlib import Path

    return load_config(env_for(Path(tmp_path), **overrides))


# --------------------------------------------------------------------- fakes
class FakeAI:
    """Deterministic AI double. Every call is recorded for assertions."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.extraction_queue: list[Any] = []
        self.correction_queue: list[Any] = []
        self.intent_queue: list[Any] = []
        self.narratives: list[Any] = []
        self.extraction_requests: list[Any] = []
        self.correction_requests: list[Any] = []
        self.intent_requests: list[Any] = []
        self.narrative_requests: list[Any] = []
        self.extraction_error: Exception | None = None
        self.narrative_error: Exception | None = None

    # queuing helpers ------------------------------------------------------
    def queue_extraction(self, extraction: Extraction | dict, *, message_local_date: date = TEHRAN_TODAY) -> None:
        if isinstance(extraction, dict):
            self.extraction_queue.append(
                validate_extraction(extraction, message_local_date=message_local_date)
            )
        else:
            self.extraction_queue.append(extraction)

    def queue_correction(self, patch: dict, *, reason: str | None = None, message_local_date: date = TEHRAN_TODAY) -> None:
        payload = {"patch": patch, "reason": reason}
        self.correction_queue.append(
            validate_correction(payload, message_local_date=message_local_date)
        )

    def queue_intent(self, **overrides: Any) -> None:
        base = {
            "intent": "report",
            "period_start": TEHRAN_TODAY.isoformat(),
            "period_end": TEHRAN_TODAY.isoformat(),
            "focus": "all",
            "needs_clarification": False,
            "question": None,
        }
        base.update(overrides)
        self.intent_queue.append(ManagerIntent(**base))

    def queue_narrative(self, text: str) -> None:
        self.narratives.append(text)

    # protocol -------------------------------------------------------------
    def extract_event(self, request):
        self.extraction_requests.append(request)
        if self.extraction_error is not None:
            raise self.extraction_error
        if not self.extraction_queue:
            raise AIUnavailable("no queued extraction in test")
        item = self.extraction_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def interpret_correction(self, *, snapshot, correction_text, message_local_date):
        self.correction_requests.append(
            {"snapshot": snapshot, "text": correction_text, "date": message_local_date}
        )
        if not self.correction_queue:
            raise AIInvalidOutput("no queued correction in test")
        item = self.correction_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def interpret_manager_request(self, *, text, today_local):
        self.intent_requests.append({"text": text, "today": today_local})
        if not self.intent_queue:
            raise AIInvalidOutput("no queued intent in test")
        item = self.intent_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def narrate_report(self, *, facts):
        self.narrative_requests.append(facts)
        if self.narrative_error is not None:
            raise self.narrative_error
        if not self.narratives:
            raise AIUnavailable("no queued narrative in test")
        return self.narratives.pop(0)


class FakeTelegram:
    """Records outgoing messages and can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.webhook_info: dict[str, Any] = {}
        self.webhook_calls: list[dict[str, Any]] = []
        self._next_id = 5000

    def queue_failure(self, exc: Exception) -> None:
        self.failures.append(exc)

    def send_message(self, *, chat_id: int, text: str, reply_to_message_id: int | None = None):
        if self.failures:
            raise self.failures.pop(0)
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "message_id": self._next_id,
            }
        )
        from app.telegram.client import SendResult

        return SendResult(message_id=self._next_id, chunk_count=max(1, len(text) // 3800 + 1))

    def messages_to(self, chat_id: int) -> list[dict[str, Any]]:
        return [message for message in self.sent if message["chat_id"] == chat_id]

    def get_webhook_info(self):
        return dict(self.webhook_info)

    def set_webhook(self, *, url, secret_token, allowed_updates=None):
        self.webhook_calls.append({"url": url, "secret": secret_token, "allowed": allowed_updates})
        return {"ok": True}

    def delete_webhook(self, *, drop_pending_updates=False):
        self.webhook_calls.append({"deleted": True})
        return {"ok": True}


# ------------------------------------------------------------------ factories
def extraction_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "classification": "business_event",
        "event_type": "sale_revenue",
        "confidence": 0.92,
        "event_date": TEHRAN_TODAY.isoformat(),
        "amount": "1500000",
        "currency": "تومان",
        "item_name": None,
        "quantity": None,
        "unit": None,
        "inventory_direction": None,
        "description": "فروش روزانه",
        "missing_fields": [],
        "ambiguities": [],
        "reason": None,
    }
    base.update(overrides)
    return base


def sale_event(**overrides: Any) -> dict[str, Any]:
    return extraction_payload(**overrides)


def expense_event(**overrides: Any) -> dict[str, Any]:
    payload = extraction_payload(event_type="expense_purchase", description="خرید از تامین‌کننده")
    payload.update(overrides)
    return payload


def inventory_event(**overrides: Any) -> dict[str, Any]:
    payload = extraction_payload(
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="آرد",
        quantity="20",
        unit="کیلوگرم",
        inventory_direction="received",
        description="ورود آرد",
    )
    payload.update(overrides)
    return payload


def note_event(**overrides: Any) -> dict[str, Any]:
    payload = extraction_payload(
        event_type="operational_note",
        amount=None,
        currency=None,
        description="یخچال خراب شد و تعمیر شد",
    )
    payload.update(overrides)
    return payload


def non_business(**overrides: Any) -> dict[str, Any]:
    payload = extraction_payload(
        classification="non_business",
        event_type=None,
        amount=None,
        currency=None,
        description=None,
        confidence=0.9,
    )
    payload.update(overrides)
    return payload


def unclear(**overrides: Any) -> dict[str, Any]:
    payload = extraction_payload(
        classification="unclear",
        event_type=None,
        amount=None,
        currency=None,
        description=None,
        missing_fields=[],
        confidence=0.4,
    )
    payload.update(overrides)
    return payload


def telegram_message(
    *,
    update_id: int,
    text: str | None,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
    sender_id: int | None = EMPLOYEE_ID,
    message_id: int | None = None,
    reply_to: int | None = None,
    is_bot: bool = False,
    unix_date: int = 1766484000,  # 2025-12-23T10:00:00Z
    **media: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id if message_id is not None else 1000 + update_id,
        "date": unix_date,
        "chat": {"id": chat_id, "type": chat_type},
    }
    if sender_id is not None:
        message["from"] = {"id": sender_id, "is_bot": is_bot, "first_name": "Test"}
    if text is not None:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "from": {"id": 42, "is_bot": True}}
    message.update(media)
    return {"update_id": update_id, "message": message}


def private_update(update_id: int, text: str, *, sender_id: int = EMPLOYEE_ID, **kwargs: Any) -> dict[str, Any]:
    return telegram_message(
        update_id=update_id,
        text=text,
        chat_id=sender_id,
        chat_type="private",
        sender_id=sender_id,
        **kwargs,
    )


def group_update(update_id: int, text: str, *, sender_id: int = EMPLOYEE_ID, **kwargs: Any) -> dict[str, Any]:
    return telegram_message(update_id=update_id, text=text, sender_id=sender_id, **kwargs)


# ------------------------------------------------------------------- plumbing
def build_context(tmp_path, *, ai: FakeAI | None = None, telegram: FakeTelegram | None = None, **overrides: str | None) -> AppContext:
    config = make_config(tmp_path, **overrides)
    db = Database(config.database_path)
    db.ensure_schema()
    return AppContext(
        config=config,
        db=db,
        ai=ai or FakeAI(),
        telegram=telegram or FakeTelegram(),
        clock=lambda: FIXED_NOW,
    )


def enqueue(ctx: AppContext, update: dict[str, Any], *, now: datetime | None = None) -> int:
    from app.repo import inbox

    received = now or FIXED_NOW
    with ctx.db.session() as conn:
        _inserted, inbox_id = inbox.enqueue(
            conn,
            update_id=update["update_id"],
            payload_json=json.dumps(update, ensure_ascii=False),
            kind="message",
            now_iso=to_utc_iso(received),
        )
    return inbox_id


def process_once(ctx: AppContext, update: dict[str, Any], *, now: datetime | None = None):
    """Enqueue one update and drain it with the real worker."""

    from app.services.worker import run_worker

    enqueue(ctx, update)
    with ctx.db.session() as conn:
        return run_worker(ctx, conn, now=now or FIXED_NOW, collect_reports=False)

