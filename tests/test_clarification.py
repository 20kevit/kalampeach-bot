"""TEST-PLAN D: private clarification, delivery constraints, pending handling."""

from __future__ import annotations

import json

import pytest

from kalampeach.services import messages
from kalampeach.telegram.client import TelegramError
from conftest import (
    EMPLOYEE_ID,
    GROUP_ID,
    OTHER_EMPLOYEE_ID,
    clarification_answer,
    extraction,
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


def _ambiguous_expense(ctx, fake_ai, *, update_id=1, message_id=10):
    fake_ai.extractions.append(
        extraction(event_type="expense_purchase", currency="تومان", description="خرید زعفران")
    )
    process(
        ctx,
        telegram_update(update_id=update_id, message_id=message_id, text="برای خرید زعفران هزینه کردیم"),
        skip_reports=True,
    )
    return clarifications(ctx)[0]


def _prompt_message_id(ctx):
    rows = [row for row in bot_messages(ctx) if row["kind"] == "clarification_prompt"]
    assert rows, "a clarification prompt should have been sent"
    return rows[-1]["message_id"]


def test_group_ambiguity_asks_the_author_privately_and_not_the_group(ctx, fake_ai, fake_telegram):
    _ambiguous_expense(ctx, fake_ai)

    assert fake_telegram.texts_to(GROUP_ID) == []
    private = "\n".join(fake_telegram.texts_to(EMPLOYEE_ID))
    assert "مبلغ" in private
    # No business details leak into the private question either.
    assert "زعفران" not in private and "خرید" not in private

    row = clarifications(ctx)[0]
    assert row["status"] == "awaiting_answer"
    assert row["question_count"] == 1
    assert row["employee_telegram_user_id"] == EMPLOYEE_ID


def test_answer_resolves_only_the_linked_clarification(ctx, fake_ai, fake_telegram):
    _ambiguous_expense(ctx, fake_ai)
    prompt = _prompt_message_id(ctx)

    fake_ai.clarification_answers.append(
        clarification_answer(values={"amount": "45000", "currency": "تومان"})
    )
    process(
        ctx,
        private_update(update_id=2, message_id=11, text="۴۵ هزار تومان", reply_to=prompt),
        skip_reports=True,
    )

    row = events(ctx)[0]
    assert row["status"] == "confirmed"
    assert row["amount"] == "45000"
    assert row["current_revision_number"] == 2
    assert clarifications(ctx)[0]["status"] == "resolved"
    assert [rev["change_type"] for rev in revisions(ctx, row["id"])] == ["created", "clarification"]
    assert fake_telegram.texts_to(EMPLOYEE_ID)[-1].startswith("✅")


def test_answer_does_not_overwrite_values_that_were_already_known(ctx, fake_ai):
    fake_ai.extractions.append(
        extraction(event_type="expense_purchase", amount="12000", currency=None)
    )
    process(
        ctx,
        telegram_update(update_id=1, message_id=10, text="۱۲ هزار تومان خرید"),
        skip_reports=True,
    )
    assert clarifications(ctx)[0]["status"] == "awaiting_answer"
    prompt = _prompt_message_id(ctx)

    # The employee answers only the currency question.
    fake_ai.clarification_answers.append(
        clarification_answer(values={"currency": "تومان", "amount": "99999"})
    )
    process(
        ctx,
        private_update(update_id=2, message_id=11, text="تومان", reply_to=prompt),
        skip_reports=True,
    )

    row = events(ctx)[0]
    assert row["amount"] == "12000", "a clarification answer must not silently change a known amount"
    assert row["currency"] == "تومان"
    assert row["status"] == "confirmed"


def test_employee_who_never_started_the_bot_gets_no_business_details(
    ctx, fake_ai, fake_telegram
):
    fake_telegram.queue_failure(TelegramError("recipient_unavailable"))
    _ambiguous_expense(ctx, fake_ai)

    row = clarifications(ctx)[0]
    assert row["status"] == "unable_to_deliver"
    group_text = "\n".join(fake_telegram.texts_to(GROUP_ID))
    assert messages.GROUP_SETUP_HINT in group_text
    assert "زعفران" not in group_text and "هزینه" not in group_text


def test_transient_delivery_failure_is_retried(ctx, fake_ai, fake_telegram):
    fake_telegram.queue_failure(TelegramError("telegram_unavailable", retryable=True))
    _ambiguous_expense(ctx, fake_ai)

    row = clarifications(ctx)[0]
    assert row["status"] == "pending_delivery"
    assert row["delivery_attempts"] == 1
    assert row["next_delivery_attempt_at_utc"] is not None

    ctx.clock.advance(minutes=10)
    run_worker(ctx, skip_reports=True)
    row = clarifications(ctx)[0]
    assert row["status"] == "awaiting_answer"
    assert row["question_count"] == 1


def test_answer_from_someone_else_is_ignored(ctx, fake_ai):
    _ambiguous_expense(ctx, fake_ai)
    prompt = _prompt_message_id(ctx)

    process(
        ctx,
        private_update(
            update_id=2,
            message_id=11,
            text="۵۰۰۰ تومان",
            reply_to=prompt,
            sender_id=OTHER_EMPLOYEE_ID,
            chat_id=OTHER_EMPLOYEE_ID,
        ),
        skip_reports=True,
    )

    assert fake_ai.calls == ["extract_event"], "no AI call for an unauthorized answer"
    assert events(ctx)[0]["status"] == "pending_clarification"
    assert clarifications(ctx)[0]["status"] == "awaiting_answer"


def test_unresolved_clarification_stays_pending_and_is_excluded_from_totals(ctx, fake_ai):
    _ambiguous_expense(ctx, fake_ai)

    from kalampeach.services import reporting

    with ctx.db.connection() as connection:
        facts = reporting.compute_facts(
            connection,
            period=reporting.Period(
                start=__import__("datetime").date(2026, 11, 23),
                end=__import__("datetime").date(2026, 11, 23),
            ),
            timezone_name="Asia/Tehran",
            generated_at_utc=ctx.now_iso(),
        )
    assert facts["counts"]["confirmed"] == 0
    assert facts["counts"]["pending_clarification"] == 1
    assert facts["has_confirmed_events"] is False
    assert facts["totals"]["expense_purchase"] == {}


def test_incomplete_second_answer_is_expired_after_two_questions(ctx, fake_ai, fake_telegram):
    _ambiguous_expense(ctx, fake_ai)  # missing amount, currency
    first_prompt = _prompt_message_id(ctx)

    # First answer resolves the currency only -> one more question is allowed.
    fake_ai.clarification_answers.append(clarification_answer(values={"currency": "تومان"}))
    process(
        ctx,
        private_update(update_id=2, message_id=11, text="تومان", reply_to=first_prompt),
        skip_reports=True,
    )
    row = clarifications(ctx)[0]
    assert row["status"] == "awaiting_answer", "a second, narrower question is asked"
    assert row["question_count"] == 2
    second_prompt = _prompt_message_id(ctx)
    assert second_prompt != first_prompt

    # Second answer still lacks the amount -> give up, stay pending, no fabrication.
    fake_ai.clarification_answers.append(clarification_answer(values={}))
    process(
        ctx,
        private_update(update_id=3, message_id=12, text="نمیدانم", reply_to=second_prompt),
        skip_reports=True,
    )

    assert clarifications(ctx)[0]["status"] == "expired"
    event = events(ctx)[0]
    assert event["status"] == "pending_clarification"
    assert event["amount"] is None
    assert messages.CLARIFICATION_EXPIRED in "\n".join(fake_telegram.texts_to(EMPLOYEE_ID))


def test_group_reply_to_the_original_message_completes_the_pending_event(
    ctx, fake_ai, fake_telegram
):
    _ambiguous_expense(ctx, fake_ai)

    fake_ai.clarification_answers.append(
        clarification_answer(values={"amount": "30000", "currency": "تومان"})
    )
    process(
        ctx,
        telegram_update(update_id=2, message_id=11, text="۳۰ هزار تومان", reply_to=10),
        skip_reports=True,
    )

    # The reply resolves to the pending event and its open clarification, so the
    # message is used as the answer. The confirmation stays private.
    row = events(ctx)[0]
    assert row["status"] == "confirmed"
    assert row["amount"] == "30000"
    assert fake_ai.calls == ["extract_event", "complete_clarification"]
    group_messages = fake_telegram.texts_to(GROUP_ID)
    assert group_messages == [], "no business details may be posted in the group"


def test_prompt_is_recorded_for_reply_resolution(ctx, fake_ai):
    _ambiguous_expense(ctx, fake_ai)
    rows = [row for row in bot_messages(ctx) if row["kind"] == "clarification_prompt"]
    assert len(rows) == 1
    assert rows[0]["clarification_id"] == clarifications(ctx)[0]["id"]
    assert rows[0]["chat_id"] == EMPLOYEE_ID


def test_undeliverable_question_is_not_retried_forever(ctx, fake_ai, fake_telegram):
    fake_telegram.queue_failure(TelegramError("recipient_blocked"))
    _ambiguous_expense(ctx, fake_ai)
    ctx.clock.advance(hours=2)
    summary = run_worker(ctx, skip_reports=True)
    assert summary["clarifications"].get("sent", 0) == 0
    assert clarifications(ctx)[0]["status"] == "unable_to_deliver"
    employee_attempts = [item for item in fake_telegram.attempts if item["chat_id"] == EMPLOYEE_ID]
    assert len(employee_attempts) == 1, "a permanently blocked recipient is asked only once"
