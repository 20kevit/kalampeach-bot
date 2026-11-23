"""Classification, extraction validation and automatic capture (TEST-PLAN C)."""

from __future__ import annotations

import json

from app.errors import AIInvalidOutput, AIUnavailable
from tests.helpers import (
    EMPLOYEE_ID,
    GROUP_ID,
    build_context,
    expense_event,
    extraction_payload,
    group_update,
    inventory_event,
    non_business,
    note_event,
    process_once,
    sale_event,
    telegram_message,
    unclear,
)


def _events(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id").fetchall()]


def _source_rows(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM source_messages").fetchall()]


def _inbox(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM telegram_inbox").fetchall()]


def test_clear_sale_is_saved_once(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(amount="1500000", currency="تومان"))
    stats = process_once(ctx, group_update(1, "فروش ۱٫۵ میلیون تومان پسته"))
    events = _events(ctx)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "sale_revenue"
    assert event["status"] == "confirmed"
    assert event["amount"] == "1500000"
    assert event["currency"] == "تومان"
    assert event["event_date"] == "2025-12-23"
    assert stats.done == 1
    assert _source_rows(ctx)[0]["processing_status"] == "processed"


def test_clear_expense_is_distinct_from_sale(ctx, fake_ai):
    fake_ai.queue_extraction(expense_event(amount="250000", currency="تومان"))
    process_once(ctx, group_update(1, "خرید ۲۵۰ هزار تومان از تامین‌کننده"))
    event = _events(ctx)[0]
    assert event["event_type"] == "expense_purchase"
    assert event["status"] == "confirmed"


def test_inventory_movement_keeps_item_quantity_unit_and_direction(ctx, fake_ai):
    fake_ai.queue_extraction(inventory_event(quantity="20", unit="کیلوگرم", item_name="آرد"))
    process_once(ctx, group_update(1, "۲۰ کیلو آرد گرفتیم"))
    event = _events(ctx)[0]
    assert event["event_type"] == "inventory_movement"
    assert event["item_name"] == "آرد"
    assert event["quantity"] == "20"
    assert event["unit"] == "کیلوگرم"
    assert event["inventory_direction"] == "received"
    assert event["amount"] is None
    assert event["currency"] is None


def test_operational_note_is_saved(ctx, fake_ai):
    fake_ai.queue_extraction(note_event())
    process_once(ctx, group_update(1, "یخچال خراب شد و تعمیر شد"))
    event = _events(ctx)[0]
    assert event["event_type"] == "operational_note"
    assert event["description"]
    assert event["amount"] is None


def test_ordinary_conversation_creates_no_event_and_no_message(ctx, fake_ai):
    fake_ai.queue_extraction(non_business())
    process_once(ctx, group_update(1, "سلام، صبح بخیر"))
    assert _events(ctx) == []
    assert _source_rows(ctx)[0]["processing_status"] == "ignored"
    assert ctx.telegram.sent == []


def test_unclear_classification_is_not_forced_into_a_category(ctx, fake_ai):
    fake_ai.queue_extraction(unclear())
    process_once(ctx, group_update(1, "یه چیزی خریدیم فکر کنم"))
    assert _events(ctx) == []
    assert _source_rows(ctx)[0]["processing_status"] == "unclear"


def test_ambiguous_amount_creates_pending_candidate_and_private_question(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, group_update(1, "فروش انجام شد"))
    events = _events(ctx)
    assert len(events) == 1
    assert events[0]["status"] == "pending_clarification"
    assert events[0]["amount"] is None  # never coerced to zero

    assert ctx.telegram.messages_to(GROUP_ID) == []
    questions = ctx.telegram.messages_to(EMPLOYEE_ID)
    assert len(questions) == 1
    assert "مبلغ" in questions[0]["text"]

    with ctx.db.session() as conn:
        clarifications = [dict(row) for row in conn.execute("SELECT * FROM clarifications")]
        revisions = [dict(row) for row in conn.execute("SELECT * FROM event_revisions")]
    assert len(clarifications) == 1
    assert clarifications[0]["status"] == "open"
    assert clarifications[0]["prompt_message_id"] is not None
    assert revisions[0]["validation_status"] == "pending_clarification"


def test_missing_currency_without_default_asks_instead_of_guessing(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(currency=None))
    process_once(ctx, group_update(1, "فروش ۵۰۰ هزار"))
    event = _events(ctx)[0]
    assert event["status"] == "pending_clarification"
    assert event["currency"] is None
    assert "واحد پول" in ctx.telegram.messages_to(EMPLOYEE_ID)[0]["text"]


def test_configured_default_currency_is_applied_and_recorded(tmp_path):
    ctx = build_context(tmp_path, DEFAULT_CURRENCY="تومان")
    ctx.ai.queue_extraction(sale_event(currency=None))
    process_once(ctx, group_update(1, "فروش ۵۰۰ هزار"))
    with ctx.db.session() as conn:
        event = dict(conn.execute("SELECT * FROM events").fetchone())
    assert event["status"] == "confirmed"
    assert event["currency"] == "تومان"
    assert json.loads(event["details_json"])["default_currency_applied"] is True


def test_explicit_currency_is_preserved_exactly(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(currency="USD", amount="12.5"))
    process_once(ctx, group_update(1, "فروش ۱۲.۵ دلار"))
    event = _events(ctx)[0]
    assert event["currency"] == "USD"
    assert event["amount"] == "12.5"


def test_explicit_currency_wins_over_default(tmp_path):
    ctx = build_context(tmp_path, DEFAULT_CURRENCY="تومان")
    ctx.ai.queue_extraction(sale_event(currency="یورو", amount="10"))
    process_once(ctx, group_update(1, "فروش ۱۰ یورو"))
    assert _events(ctx)[0]["currency"] == "یورو"


def test_malformed_ai_output_is_rejected_without_event(ctx, fake_ai):
    fake_ai.queue_extraction(AIInvalidOutput("fenced markdown"))
    process_once(ctx, group_update(1, "فروش ۵۰۰ هزار تومان"))
    assert _events(ctx) == []
    row = _inbox(ctx)[0]
    assert row["status"] == "failed"
    assert row["last_error_category"] == "AI_INVALID_OUTPUT"
    assert _source_rows(ctx)[0]["message_text"] == "فروش ۵۰۰ هزار تومان"


def test_ai_outage_preserves_message_and_retries_without_fabricating(ctx, fake_ai):
    fake_ai.extraction_error = AIUnavailable("provider timeout")
    process_once(ctx, group_update(1, "فروش ۵۰۰ هزار تومان"))
    assert _events(ctx) == []
    row = _inbox(ctx)[0]
    assert row["status"] == "retry"
    assert row["last_error_category"] == "AI_UNAVAILABLE"
    assert _source_rows(ctx)[0]["message_text"] == "فروش ۵۰۰ هزار تومان"

    # Recovery: the provider comes back and the queued update completes.
    fake_ai.extraction_error = None
    fake_ai.queue_extraction(sale_event(amount="500000"))
    from app.services.worker import run_worker
    from tests.helpers import FIXED_NOW

    from datetime import timedelta

    with ctx.db.session() as conn:
        # The retry backoff must elapse before the row is due again.
        run_worker(ctx, conn, now=FIXED_NOW + timedelta(seconds=60), collect_reports=False)
    assert len(_events(ctx)) == 1
    assert _inbox(ctx)[0]["status"] == "done"


def test_duplicate_telegram_update_creates_at_most_one_event(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    first = group_update(1, "فروش ۵۰۰ هزار تومان", message_id=777)
    duplicate = group_update(2, "فروش ۵۰۰ هزار تومان", message_id=777)
    process_once(ctx, first)
    process_once(ctx, duplicate)
    assert len(_events(ctx)) == 1
    # Only the first processing consumed an AI extraction.
    assert len(fake_ai.extraction_requests) == 1


def test_reprocessing_an_already_captured_message_does_not_duplicate(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    update = group_update(1, "فروش ۵۰۰ هزار تومان")
    process_once(ctx, update)

    # Simulate a second, distinct update for the very same Telegram message
    # (for example a webhook redelivery with a new update_id after a restart).
    from app.services.worker import run_worker
    from tests.helpers import FIXED_NOW

    second = group_update(2, "فروش ۵۰۰ هزار تومان", message_id=update["message"]["message_id"])
    from tests.helpers import enqueue

    enqueue(ctx, second)
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)

    assert len(_events(ctx)) == 1
    assert len(fake_ai.extraction_requests) == 1


def test_amounts_are_stored_as_canonical_decimal_strings(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(amount="0.10", currency="USD"))
    process_once(ctx, group_update(1, "فروش"))
    fake_ai.queue_extraction(sale_event(amount="0.20", currency="USD", description="دوم"))
    process_once(ctx, group_update(2, "فروش دوم"))
    amounts = [event["amount"] for event in _events(ctx)]
    assert amounts == ["0.1", "0.2"]


def test_group_acknowledgements_can_be_disabled(tmp_path):
    ctx = build_context(tmp_path, SEND_GROUP_ACKNOWLEDGEMENTS="false")
    ctx.ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    assert ctx.telegram.sent == []
    assert len(_events(ctx)) == 1


def test_group_acknowledgement_replies_to_the_source_message(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۱۰۰ تومان", message_id=444))
    ack = ctx.telegram.messages_to(GROUP_ID)[0]
    assert ack["reply_to_message_id"] == 444
    assert "ثبت شد" in ack["text"]


def test_extraction_with_unknown_event_type_creates_pending_candidate(ctx, fake_ai):
    fake_ai.queue_extraction(
        extraction_payload(event_type=None, amount="100", currency="تومان", missing_fields=["event_type"])
    )
    process_once(ctx, group_update(1, "۱۰۰ تومان ثبت کن"))
    event = _events(ctx)[0]
    assert event["status"] == "pending_clarification"
    assert event["event_type"] is None
    question = ctx.telegram.messages_to(EMPLOYEE_ID)[0]["text"]
    assert "نوع این رویداد" in question


def test_media_message_in_group_is_not_extracted(ctx, fake_ai):
    process_once(ctx, telegram_message(update_id=1, text=None, voice={"file_id": "abc"}))
    assert fake_ai.extraction_requests == []
    assert _events(ctx) == []
