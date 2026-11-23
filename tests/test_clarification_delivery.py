"""Regression tests for the clarification delivery state machine (TEST-PLAN D).

Every test here covers a defect that was reproduced against the previous
implementation: duplicated questions under overlapping cron runs, duplicated
questions after a crash between send and bookkeeping, and the conflation of
"definitely not delivered" with "outcome unknown".
"""

from __future__ import annotations

import threading

import pytest

from kalampeach import repository
from kalampeach.services import capture as capture_service
from kalampeach.services import messages
from kalampeach.services.capture import CaptureService
from kalampeach.telegram.client import TelegramError
from kalampeach.timeutil import to_utc_iso
from conftest import EMPLOYEE_ID, GROUP_ID, clarification_answer, extraction
from helpers import (
    bot_messages,
    clarifications,
    events,
    fetch_one,
    private_update,
    process,
    rebuild_ctx,
    revisions,
    run_worker,
    seed_pending_clarification,
    telegram_update,
    with_worker,
)


def status_of(ctx, clarification_id: int) -> str:
    return str(fetch_one(ctx, "SELECT status FROM clarifications WHERE id = ?", (clarification_id,))["status"])


def row_of(ctx, clarification_id: int):
    return fetch_one(ctx, "SELECT * FROM clarifications WHERE id = ?", (clarification_id,))


def test_delivery_does_not_depend_on_the_update_inbox(ctx, fake_telegram):
    seed_pending_clarification(ctx)
    assert fetch_one(ctx, "SELECT COUNT(*) AS total FROM telegram_updates")["total"] == 0

    summary = run_worker(ctx, skip_reports=True)

    assert summary["clarifications"]["sent"] == 1
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1
    assert fetch_one(ctx, "SELECT COUNT(*) AS total FROM telegram_updates")["total"] == 0


def test_overlapping_runs_send_the_question_once(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(ctx)
    now = to_utc_iso(ctx.now())

    # Two cron runs read the due list before either has recorded delivery.
    with ctx.db.connection() as connection:
        first_view = repository.list_clarifications_for_delivery(connection, now=now)
    with ctx.db.connection() as connection:
        second_view = repository.list_clarifications_for_delivery(connection, now=now)
    assert [int(row["id"]) for row in first_view] == [clarification_id]
    assert [int(row["id"]) for row in second_view] == [clarification_id]

    service = CaptureService(ctx)
    outcomes = [
        service._deliver_one(clarification_id, ctx.now()),
        service._deliver_one(clarification_id, ctx.now()),
    ]

    assert outcomes == ["sent", "skipped"]
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1
    assert status_of(ctx, clarification_id) == "awaiting_answer"


def test_concurrent_workers_deliver_a_question_once(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(ctx)
    results = []
    errors = []

    def work(worker_id: str):
        try:
            worker_ctx = with_worker(ctx, worker_id)
            results.append(CaptureService(worker_ctx).deliver_pending_clarifications())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=("worker-%d" % index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sum(result["sent"] for result in results) == 1
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1
    assert status_of(ctx, clarification_id) == "awaiting_answer"


def test_crash_between_send_and_bookkeeping_never_resends(ctx, fake_ai, fake_telegram, monkeypatch):
    event_id, clarification_id = seed_pending_clarification(ctx)

    def crash(*args, **kwargs):
        raise RuntimeError("process killed after a successful send")

    monkeypatch.setattr(repository, "mark_clarification_delivered", crash)
    summary = run_worker(ctx, skip_reports=True)

    assert summary["clarifications"]["uncertain"] == 1
    assert status_of(ctx, clarification_id) == "delivery_uncertain"
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1
    # The unknown outcome is operator-visible and never retried.
    assert row_of(ctx, clarification_id)["last_delivery_error"] == "bookkeeping_failed"

    monkeypatch.undo()
    run_worker(ctx, skip_reports=True)
    ctx.clock.advance(hours=1)
    run_worker(ctx, skip_reports=True)
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1, "no duplicate question after a crash"

    # Reconciliation path: replying to their own message still completes the event.
    fake_ai.clarification_answers.append(
        clarification_answer(values={"amount": "45000", "currency": "تومان"})
    )
    source = fetch_one(ctx, "SELECT message_id FROM source_messages WHERE id = (SELECT source_message_id FROM events WHERE id = ?)", (event_id,))
    process(
        ctx,
        telegram_update(
            update_id=900,
            message_id=901,
            text="۴۵ هزار تومان",
            reply_to=int(source["message_id"]),
        ),
        skip_reports=True,
    )

    event = fetch_one(ctx, "SELECT * FROM events WHERE id = ?", (event_id,))
    assert event["status"] == "confirmed"
    assert event["amount"] == "45000"
    assert status_of(ctx, clarification_id) == "resolved"
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]


@pytest.mark.parametrize(
    "category", ["telegram_timeout", "telegram_invalid_response", "network_reset"]
)
def test_uncertain_outcomes_are_never_treated_as_undeliverable(ctx, fake_telegram, category):
    event_id, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError(category, uncertain=True))

    summary = run_worker(ctx, skip_reports=True)

    assert summary["clarifications"]["uncertain"] == 1
    assert status_of(ctx, clarification_id) == "delivery_uncertain"
    # No "ask the employee to press Start" hint: they may have received it.
    assert GROUP_ID not in [item["chat_id"] for item in fake_telegram.attempts]
    assert fake_telegram.texts_to(EMPLOYEE_ID) == []


def test_uncertain_question_still_accepts_an_answer_by_reply(ctx, fake_ai, fake_telegram):
    event_id, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("telegram_timeout", uncertain=True))
    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "delivery_uncertain"

    fake_ai.clarification_answers.append(
        clarification_answer(values={"amount": "7000"})
    )
    source_message_id = fetch_one(
        ctx, "SELECT message_id FROM source_messages WHERE id = (SELECT source_message_id FROM events WHERE id = ?)", (event_id,)
    )["message_id"]
    process(
        ctx,
        telegram_update(
            update_id=2, message_id=11, text="۷ هزار تومان", reply_to=source_message_id
        ),
        skip_reports=True,
    )

    event = fetch_one(ctx, "SELECT * FROM events WHERE id = ?", (event_id,))
    assert event["status"] == "confirmed"
    assert event["amount"] == "7000"
    assert fake_telegram.texts_to(GROUP_ID) == []


def test_stale_sending_state_is_swept_to_uncertain_without_resending(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(
        ctx, status="sending", lease_expires_at="2026-11-23T11:00:00Z"
    )

    summary = run_worker(ctx, skip_reports=True)

    assert summary["clarifications"]["sent"] == 0
    assert status_of(ctx, clarification_id) == "delivery_uncertain"
    assert row_of(ctx, clarification_id)["last_delivery_error"] == "lease_expired_during_send"
    assert fake_telegram.attempts == []


def test_live_sending_claim_is_left_alone(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(
        ctx, status="sending", lease_expires_at="2026-11-23T12:10:00Z"
    )

    summary = run_worker(ctx, skip_reports=True)

    assert summary["clarifications"]["sent"] == 0
    assert status_of(ctx, clarification_id) == "sending"
    assert fake_telegram.attempts == []


def test_retryable_failure_returns_to_pending_delivery(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("telegram_unavailable", retryable=True))

    run_worker(ctx, skip_reports=True)
    row = row_of(ctx, clarification_id)
    assert row["status"] == "pending_delivery"
    assert row["delivery_attempts"] == 1
    assert row["next_delivery_attempt_at_utc"] is not None
    assert row["lease_owner"] is None

    ctx.clock.advance(minutes=30)
    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "awaiting_answer"
    assert len(fake_telegram.texts_to(EMPLOYEE_ID)) == 1


def test_permanent_failure_is_undeliverable_and_never_retried(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("recipient_unavailable"))

    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "unable_to_deliver"
    assert messages.GROUP_SETUP_HINT in "\n".join(fake_telegram.texts_to(GROUP_ID))

    ctx.clock.advance(days=1)
    run_worker(ctx, skip_reports=True)
    assert len(fake_telegram.attempts) == 2, "one question attempt plus the single group hint"


def test_confirmed_event_closes_every_open_question(ctx):
    event_id, clarification_id = seed_pending_clarification(
        ctx, status="delivery_uncertain"
    )
    # A second, still-undelivered question for the same event.
    with ctx.db.transaction() as connection:
        repository.requeue_clarification_question(
            connection,
            clarification_id=clarification_id,
            question_text="مبلغ؟",
            missing_fields=["amount"],
            now=ctx.now_iso(),
        )
    service = CaptureService(ctx)
    with ctx.db.transaction() as connection:
        service._resolve_other_clarifications(connection, event_id, ctx.now_iso())

    assert status_of(ctx, clarification_id) == "resolved"
    assert fetch_one(ctx, "SELECT COUNT(*) AS total FROM bot_messages")["total"] == 0


def test_answer_requires_the_owner(ctx, fake_ai, fake_telegram):
    event_id, clarification_id = seed_pending_clarification(ctx)
    service = CaptureService(ctx)
    with ctx.db.connection() as connection:
        row = repository.get_clarification(connection, clarification_id)
    outcome = service.handle_answer(
        message=type("M", (), {"sender_id": 424242, "text": "x", "chat_id": 424242})(),
        source_message_id=1,
        clarification=row,
    )
    assert outcome.action == "ignored"
    assert fake_telegram.attempts == []


def test_uncertain_and_pending_states_are_visible_to_operators(ctx, fake_telegram):
    seed_pending_clarification(ctx, seq=1)
    fake_telegram.queue_failure(TelegramError("telegram_timeout", uncertain=True))
    run_worker(ctx, skip_reports=True)

    health = ctx.db.health()
    assert health["review_needed"]["clarifications_uncertain"] == 1
    assert health["review_needed"]["reports_uncertain"] == 0


def test_pending_delivery_is_not_answerable(ctx, fake_ai):
    """A question that was never attempted must not swallow a correction reply."""

    event_id, clarification_id = seed_pending_clarification(ctx, status="pending_delivery")
    with ctx.db.connection() as connection:
        row = repository.get_clarification(connection, clarification_id)
    assert not capture_service.clarification_accepts_answer(row, ctx.now_iso())

    with ctx.db.connection() as connection:
        row = repository.get_clarification(connection, clarification_id)
    connection_status = row["status"]
    assert connection_status == "pending_delivery"
