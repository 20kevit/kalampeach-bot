"""TEST-PLAN E: reply-based corrections, permissions, append-only revisions."""

from __future__ import annotations

import json

import pytest

from kalampeach import repository
from kalampeach.services import messages
from kalampeach.telegram.client import TelegramError
from kalampeach.timeutil import to_utc_iso
from conftest import (
    EMPLOYEE_ID,
    GROUP_ID,
    MANAGER_ID,
    OTHER_EMPLOYEE_ID,
    correction,
    extraction,
)
from helpers import (
    bot_messages,
    clarifications,
    events,
    fetch_all,
    private_update,
    process,
    revisions,
    telegram_update,
)


def _confirmed_sale(ctx, fake_ai, *, update_id=1, message_id=10, amount="100000"):
    fake_ai.extractions.append(
        extraction(amount=amount, currency="تومان", description="فروش اولیه")
    )
    process(
        ctx,
        telegram_update(update_id=update_id, message_id=message_id, text="فروش ۱۰۰ هزار تومان"),
        skip_reports=True,
    )
    return events(ctx)[0]


def test_author_can_correct_own_event_by_replying(ctx, fake_ai):
    event = _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(changes={"amount": "120000"}, reason="مبلغ اشتباه بود"))

    process(
        ctx,
        telegram_update(
            update_id=2, message_id=11, text="نه، ۱۲۰ هزار تومان بود", reply_to=10
        ),
        skip_reports=True,
    )

    updated = events(ctx)[0]
    assert updated["amount"] == "120000"
    assert updated["current_revision_number"] == 2

    history = revisions(ctx, event["id"])
    assert [row["revision_number"] for row in history] == [1, 2]
    assert history[1]["change_type"] == "correction"
    assert history[1]["actor_telegram_user_id"] == EMPLOYEE_ID
    assert json.loads(history[1]["previous_snapshot"])["amount"] == "100000"
    assert json.loads(history[1]["new_snapshot"])["amount"] == "120000"
    # The first revision is never overwritten.
    assert json.loads(history[0]["new_snapshot"])["amount"] == "100000"


def test_another_employee_cannot_correct_someone_elses_event(ctx, fake_ai, fake_telegram):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(changes={"amount": "1"}))

    process(
        ctx,
        telegram_update(
            update_id=2,
            message_id=11,
            text="۵ تومان بود",
            reply_to=10,
            sender_id=OTHER_EMPLOYEE_ID,
        ),
        skip_reports=True,
    )

    assert events(ctx)[0]["amount"] == "100000"
    assert len(revisions(ctx, events(ctx)[0]["id"])) == 1
    # The refusal is private; nothing is posted to the group.
    assert fake_telegram.texts_to(GROUP_ID) == []
    assert messages.CORRECTION_NOT_ALLOWED in fake_telegram.texts_to(OTHER_EMPLOYEE_ID)
    assert fake_ai.calls == ["extract_event"], "permission is checked before any AI call"


def test_manager_can_correct_any_event(ctx, fake_ai):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(changes={"description": "اصلاح توسط مدیر"}))

    process(
        ctx,
        telegram_update(
            update_id=2,
            message_id=11,
            text="توضیح را اصلاح کن",
            reply_to=10,
            sender_id=MANAGER_ID,
        ),
        skip_reports=True,
    )

    event = events(ctx)[0]
    assert event["description"] == "اصلاح توسط مدیر"
    assert event["current_revision_number"] == 2


def test_correction_by_replying_to_the_bot_confirmation(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="5000", currency="تومان"))
    process(ctx, private_update(update_id=1, message_id=10, text="فروش"), skip_reports=True)
    confirmation = [row for row in bot_messages(ctx) if row["kind"] == "event_confirmation"][0]

    fake_ai.corrections.append(correction(changes={"amount": "6000"}))
    process(
        ctx,
        private_update(
            update_id=2,
            message_id=11,
            text="۶ هزار تومان بود",
            reply_to=confirmation["message_id"],
        ),
        skip_reports=True,
    )

    assert events(ctx)[0]["amount"] == "6000"
    assert len(revisions(ctx, events(ctx)[0]["id"])) == 2


def test_duplicate_correction_update_is_idempotent(ctx, fake_ai):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(changes={"amount": "150000"}))
    payload = telegram_update(update_id=2, message_id=11, text="۱۵۰ هزار", reply_to=10)

    process(ctx, payload, skip_reports=True)

    from helpers import enqueue, run_worker

    enqueue(ctx, payload)
    run_worker(ctx, skip_reports=True)
    enqueue(ctx, payload)
    run_worker(ctx, skip_reports=True)

    assert events(ctx)[0]["amount"] == "150000"
    assert len(revisions(ctx, events(ctx)[0]["id"])) == 2
    assert fake_ai.calls.count("interpret_correction") == 1


def test_correction_can_complete_a_pending_event(ctx, fake_ai, fake_telegram):
    # The clarification cannot be delivered (blocked bot), so no answer is
    # linked; a reply to the source message is then interpreted as a correction.
    fake_telegram.queue_failure(TelegramError("recipient_blocked"))
    fake_ai.extractions.append(
        extraction(event_type="expense_purchase", currency="تومان", description="خرید")
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="هزینه کردیم"),
        skip_reports=True,
    )
    assert events(ctx)[0]["status"] == "pending_clarification"
    assert clarifications(ctx)[0]["status"] == "unable_to_deliver"

    fake_ai.corrections.append(correction(changes={"amount": "25000"}))
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="۲۵ هزار تومان بود", reply_to=10),
        skip_reports=True,
    )

    event = events(ctx)[0]
    assert event["status"] == "confirmed"
    assert event["amount"] == "25000"
    assert event["current_revision_number"] == 2
    # The earlier, undeliverable question keeps its factual delivery status; the
    # event itself is confirmed and no new question was created.
    assert clarifications(ctx)[0]["status"] == "unable_to_deliver"
    assert len(clarifications(ctx)) == 1


def test_correction_can_change_the_category_with_revalidation(ctx, fake_ai):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(
        correction(
            changes={"event_type": "expense_purchase", "description": "در واقع خرید بود"},
            reason="دسته اشتباه بود",
        )
    )
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="این خرید بود نه فروش", reply_to=10),
        skip_reports=True,
    )

    event = events(ctx)[0]
    assert event["event_type"] == "expense_purchase"
    assert event["status"] == "confirmed"
    # Currency/amount remained valid, so no new clarification was needed.
    assert clarifications(ctx) == []


def test_incomplete_correction_asks_privately_and_changes_nothing(ctx, fake_ai, fake_telegram):
    _confirmed_sale(ctx, fake_ai)
    # Changing the category to inventory requires item/quantity/unit, which the
    # message does not provide: the event must not be degraded or half-updated.
    fake_ai.corrections.append(correction(changes={"event_type": "inventory_movement"}))
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="این حرکت موجودی بود", reply_to=10),
        skip_reports=True,
    )

    event = events(ctx)[0]
    assert event["event_type"] == "sale_revenue", "an incomplete correction changes nothing"
    assert event["amount"] == "100000"
    assert event["current_revision_number"] == 1

    pending = clarifications(ctx)
    assert len(pending) == 1
    assert pending[0]["status"] == "awaiting_answer"
    assert json.loads(pending[0]["missing_fields"]) == ["item_name", "quantity", "unit", "inventory_direction"]
    assert fake_telegram.texts_to(GROUP_ID) == []


def test_not_a_correction_is_ignored(ctx, fake_ai):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(interpretation="not_a_correction", changes={}))
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="ممنون", reply_to=10),
        skip_reports=True,
    )
    assert events(ctx)[0]["amount"] == "100000"
    assert len(revisions(ctx, events(ctx)[0]["id"])) == 1


def test_unclear_correction_gets_a_private_hint(ctx, fake_ai, fake_telegram):
    _confirmed_sale(ctx, fake_ai)
    fake_ai.corrections.append(correction(interpretation="unclear", changes={}))
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="هوم", reply_to=10),
        skip_reports=True,
    )
    assert messages.CORRECTION_UNCLEAR in fake_telegram.texts_to(EMPLOYEE_ID)
    assert fake_telegram.texts_to(GROUP_ID) == []


def test_unresolved_reply_target_is_reported_privately(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(extraction(amount="100", currency="تومان"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش"), skip_reports=True)

    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="اصلاحش کن", reply_to=999),
        skip_reports=True,
    )
    assert messages.REPLY_TARGET_UNRESOLVED in fake_telegram.texts_to(EMPLOYEE_ID)
    assert fake_telegram.texts_to(GROUP_ID) == []
    assert events(ctx)[0]["amount"] == "100"


def test_ambiguous_reply_target_asks_instead_of_guessing(ctx, fake_ai, fake_telegram):
    # Two events, then one source message that both revisions point at.
    _confirmed_sale(ctx, fake_ai, update_id=1, message_id=10, amount="100")
    _confirmed_sale(ctx, fake_ai, update_id=2, message_id=11, amount="200")
    with ctx.db.transaction() as connection:
        shared, _ = repository.record_source_message(
            connection,
            telegram_update_id=99,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=50,
            reply_to_message_id=None,
            sender_telegram_user_id=EMPLOYEE_ID,
            message_text="پیام مبهم",
            message_timestamp_utc=to_utc_iso(ctx.now()),
            processing_status="processed",
            now=to_utc_iso(ctx.now()),
        )
        for row in events(ctx):
            repository.insert_revision(
                connection,
                event_id=int(row["id"]),
                revision_number=repository.next_revision_number(connection, int(row["id"])),
                actor_telegram_user_id=EMPLOYEE_ID,
                change_type="correction",
                change_reason="test",
                source_message_id=int(shared["id"]),
                clarification_id=None,
                previous_snapshot=None,
                new_snapshot="{}",
                validation_status="accepted",
                now=to_utc_iso(ctx.now()),
            )

    process(
        ctx,
        telegram_update(update_id=3, message_id=12, text="اصلاح", reply_to=50),
        skip_reports=True,
    )

    assert messages.REPLY_TARGET_UNRESOLVED in fake_telegram.texts_to(EMPLOYEE_ID)
    assert fake_ai.calls == ["extract_event", "extract_event"]
    assert [row["amount"] for row in events(ctx)] == ["100", "200"]


def test_revisions_are_append_only(ctx, fake_ai):
    _confirmed_sale(ctx, fake_ai)
    for index, amount in enumerate(["110000", "120000", "130000"], start=1):
        fake_ai.corrections.append(correction(changes={"amount": amount}))
        process(
            ctx,
            telegram_update(
                update_id=1 + index,
                message_id=10 + index,
                text="اصلاح %s" % amount,
                reply_to=10,
            ),
            skip_reports=True,
        )

    history = revisions(ctx, events(ctx)[0]["id"])
    assert [row["revision_number"] for row in history] == [1, 2, 3, 4]
    assert [json.loads(row["new_snapshot"])["amount"] for row in history] == [
        "100000",
        "110000",
        "120000",
        "130000",
    ]
    with ctx.db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) AS total FROM event_revisions").fetchone()
    assert count["total"] == 4
