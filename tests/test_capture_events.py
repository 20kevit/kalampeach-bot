"""TEST-PLAN C: classification, extraction, and event validation."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kalampeach.ai.types import AIUnavailable
from kalampeach.services import messages
from conftest import (
    EMPLOYEE_ID,
    extraction,
    non_business_extraction,
)
from helpers import (
    bot_messages,
    clarifications,
    enqueue,
    events,
    fetch_one,
    private_update,
    process,
    rebuild_ctx,
    revisions,
    run_worker,
    telegram_update,
)


def test_clear_sale_creates_one_correct_event(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(amount="125000", currency="تومان", description="فروش رب گوجه")
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="۱۲۵ هزار تومان رب فروختیم"),
        skip_reports=True,
    )

    rows = events(ctx)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "sale_revenue"
    assert row["status"] == "confirmed"
    assert row["amount"] == "125000"
    assert row["currency"] == "تومان"
    assert row["event_date"] == "2026-11-23"  # message's Tehran business date
    assert row["currency_source"] == "explicit"
    assert row["created_by_telegram_user_id"] == EMPLOYEE_ID

    history = revisions(ctx, row["id"])
    assert len(history) == 1
    assert history[0]["change_type"] == "created"
    assert history[0]["validation_status"] == "accepted"


def test_expense_is_never_labeled_as_a_sale(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(event_type="expense_purchase", amount="50000", currency="تومان")
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="۵۰ هزار تومان مواد خریدیم"),
        skip_reports=True,
    )
    row = events(ctx)[0]
    assert row["event_type"] == "expense_purchase"
    assert row["amount"] == "50000"


def test_inventory_movement_preserves_item_quantity_unit_and_direction(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(
            event_type="inventory_movement",
            item_name="رب گوجه",
            quantity="12.5",
            unit="کیلوگرم",
            inventory_direction="consumed",
            description="مصرف آشپزخانه",
        )
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="۱۲.۵ کیلو رب مصرف شد"),
        skip_reports=True,
    )
    row = events(ctx)[0]
    assert row["event_type"] == "inventory_movement"
    assert row["item_name"] == "رب گوجه"
    assert row["quantity"] == "12.5"
    assert row["unit"] == "کیلوگرم"
    assert row["inventory_direction"] == "consumed"
    assert row["amount"] is None, "inventory must not gain an inferred monetary value"


def test_operational_incident_creates_a_note(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(event_type="operational_note", description="یخچال خراب شد و تعمیر شد")
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="یخچال خراب شد و تعمیر شد"),
        skip_reports=True,
    )
    row = events(ctx)[0]
    assert row["event_type"] == "operational_note"
    assert row["amount"] is None
    assert row["currency"] is None


def test_ordinary_conversation_creates_no_event_and_no_message(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(non_business_extraction())
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="سلام چطوری؟"),
        skip_reports=True,
    )
    assert events(ctx) == []
    assert fake_telegram.sent == []


def test_unclear_private_message_gets_a_private_hint(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(non_business_extraction("unclear"))
    process(
        ctx, private_update(update_id=1, message_id=10, text="همون قبلیه"), skip_reports=True
    )
    assert events(ctx) == []
    assert fake_telegram.texts_to(EMPLOYEE_ID) == [messages.UNCLEAR_MESSAGE]


def test_ambiguous_amount_stays_pending_and_asks_privately(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(
        extraction(event_type="expense_purchase", currency="تومان", description="خرید مواد")
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="برای مواد هزینه کردیم"),
        skip_reports=True,
    )

    rows = events(ctx)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending_clarification"
    assert rows[0]["amount"] is None, "unknown amount must stay NULL, never 0"

    pending = clarifications(ctx)
    assert len(pending) == 1
    assert pending[0]["status"] == "awaiting_answer"
    assert json.loads(pending[0]["missing_fields"]) == ["amount"]

    # Clarification is private to the author; nothing goes to the group.
    assert fake_telegram.texts_to(GROUP_ID_PLACEHOLDER := -1001234567890) == []
    private_text = "\n".join(fake_telegram.texts_to(EMPLOYEE_ID))
    assert "مبلغ" in private_text
    assert "رب" not in private_text


def test_unknown_amount_is_never_stored_as_zero(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount=None, currency="تومان"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش داشتیم"), skip_reports=True)
    row = events(ctx)[0]
    assert row["amount"] is None
    assert row["status"] == "pending_clarification"


def test_zero_amount_is_rejected_as_invalid(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="0", currency="تومان"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش صفر"), skip_reports=True)
    row = events(ctx)[0]
    assert row["status"] == "pending_clarification"
    assert "amount" in row["status_reason"]


def test_currency_is_preserved_exactly(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="20", currency="USD"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="20 دلار فروش"), skip_reports=True)
    assert events(ctx)[0]["currency"] == "USD"


def test_missing_currency_without_default_stays_pending(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="900", currency=None))
    process(ctx, telegram_update(update_id=1, message_id=10, text="۹۰۰ فروش"), skip_reports=True)
    row = events(ctx)[0]
    assert row["status"] == "pending_clarification"
    assert row["currency"] is None
    assert "currency" in row["status_reason"]


def test_configured_default_currency_is_applied_and_recorded(ctx, fake_ai):
    ctx = rebuild_ctx(ctx, default_currency="تومان")
    fake_ai.extractions.append(extraction(amount="900", currency=None))
    process(ctx, telegram_update(update_id=1, message_id=10, text="۹۰۰ فروش"), skip_reports=True)
    row = events(ctx)[0]
    assert row["status"] == "confirmed"
    assert row["currency"] == "تومان"
    assert row["currency_source"] == "default"


def test_inventory_missing_unit_stays_pending(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(
            event_type="inventory_movement",
            item_name="برنج",
            quantity="5",
            unit=None,
            inventory_direction="received",
        )
    )
    process(ctx, telegram_update(update_id=1, message_id=10, text="۵ برنج گرفتیم"), skip_reports=True)
    row = events(ctx)[0]
    assert row["status"] == "pending_clarification"
    assert "unit" in row["status_reason"]


def test_note_without_description_stays_pending(ctx, fake_ai):
    fake_ai.extractions.append(extraction(event_type="operational_note", description=None))
    process(ctx, telegram_update(update_id=1, message_id=10, text="یه چیزی شد"), skip_reports=True)
    assert events(ctx)[0]["status"] == "pending_clarification"


def test_implausible_date_is_not_trusted(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(amount="10", currency="تومان", event_date="2026-11-25")
    )
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش"), skip_reports=True)
    row = events(ctx)[0]
    assert row["event_date"] == "2026-11-23", "the message date is deterministic"
    assert row["status"] == "pending_clarification"
    assert "event_date" in row["status_reason"]


def test_explicit_past_date_is_preserved(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(amount="10", currency="تومان", event_date="2026-11-20")
    )
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش دیروز"), skip_reports=True)
    assert events(ctx)[0]["event_date"] == "2026-11-20"


def test_duplicate_update_creates_at_most_one_event(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="100", currency="تومان"))
    payload = telegram_update(update_id=1, message_id=10, text="فروش ۱۰۰")
    process(ctx, payload, skip_reports=True)
    enqueue(ctx, payload)
    run_worker(ctx, skip_reports=True)
    enqueue(ctx, payload)
    run_worker(ctx, skip_reports=True)

    assert len(events(ctx)) == 1
    assert len(revisions(ctx, events(ctx)[0]["id"])) == 1
    assert len(fake_ai.calls) == 1


def test_event_and_revision_writes_are_transactional(ctx, fake_ai, monkeypatch):
    from kalampeach import repository

    fake_ai.extractions.append(extraction(amount="100", currency="تومان"))
    payload = telegram_update(update_id=1, message_id=10, text="فروش ۱۰۰")
    enqueue(ctx, payload)

    original = repository.insert_revision

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(repository, "insert_revision", broken)
    summary = run_worker(ctx, skip_reports=True)
    assert summary["retried"] == 1
    assert events(ctx) == [], "a failed revision must roll back the event insert"

    monkeypatch.setattr(repository, "insert_revision", original)
    ctx.clock.advance(minutes=10)
    fake_ai.extractions.append(extraction(amount="100", currency="تومان"))
    run_worker(ctx, skip_reports=True)
    rows = events(ctx)
    assert len(rows) == 1
    assert rows[0]["amount"] == "100"


def test_ai_outage_creates_no_event_and_schedules_a_retry(ctx, fake_ai):
    fake_ai.extractions.append(AIUnavailable("provider down"))
    payload = telegram_update(update_id=1, message_id=10, text="فروش ۱۰۰")
    enqueue(ctx, payload)
    summary = run_worker(ctx, skip_reports=True)

    assert summary["retried"] == 1
    assert events(ctx) == []
    update = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert update["status"] == "queued"
    assert update["last_error_category"] == "ai_unavailable"
    source = fetch_one(ctx, "SELECT * FROM source_messages WHERE telegram_update_id = 1")
    assert source["message_text"] == "فروش ۱۰۰", "original message is preserved"


def test_permanently_invalid_ai_output_fails_the_update(ctx, fake_ai):
    from kalampeach.ai.types import AIInvalidOutput

    fake_ai.extractions.append(AIInvalidOutput("bad json"))
    payload = telegram_update(update_id=1, message_id=10, text="فروش ۱۰۰")
    enqueue(ctx, payload)
    summary = run_worker(ctx, skip_reports=True)

    assert summary["failed"] == 1
    assert events(ctx) == []
    update = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert update["status"] == "failed"
    assert update["last_error_category"] == "ai_invalid_output"
    source = fetch_one(ctx, "SELECT * FROM source_messages WHERE telegram_update_id = 1")
    assert source["processing_status"] == "failed"


def test_confirmation_is_private_only(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(extraction(amount="10", currency="تومان"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش"), skip_reports=True)
    assert fake_telegram.sent == []

    fake_ai.extractions.append(extraction(amount="20", currency="تومان"))
    process(ctx, private_update(update_id=2, message_id=11, text="فروش"), skip_reports=True)
    confirmations = [row for row in bot_messages(ctx) if row["kind"] == "event_confirmation"]
    assert len(confirmations) == 1
    assert confirmations[0]["chat_id"] == EMPLOYEE_ID
