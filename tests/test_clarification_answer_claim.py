"""Regression tests for the clarification answer claim and uncertainty fencing.

Defect 1 (answer claim)
-----------------------
At ``93c1ca7`` ``CaptureService.handle_answer`` checked
``clarification_accepts_answer`` and only then ran the AI call and wrote the
revision. Nothing proved, at write time, that *this* answer still owned the right
to resolve the question, and ``mark_clarification_answered`` was a plain
``UPDATE ... WHERE id = ?``. Two different valid answers from the same employee
could therefore both pass the check, both run the AI, and both resolve the
clarification: two clarification revisions, two confirmations, the second answer
silently overwriting the first one's amounts.

Fix: ``repository.claim_clarification_answer`` performs the check and the write in
a single conditional ``UPDATE`` filtered to the answerable states, and the claim
shares the transaction with the revision, the resolution and the inbox update.
Exactly one answer can move the row to ``answered``; a losing answer applies
nothing and is recorded as ``ignored`` / ``answer_claim_lost``.

Defect 2 (uncertainty recovery)
-------------------------------
``CaptureService._mark_uncertain_after_success`` called
``mark_clarification_delivery_uncertain`` without a worker id, and the helper
accepted ``worker_id=None`` as "skip the fence" while only filtering ``WHERE id``.
The recovery path could therefore overwrite a *resolved* clarification with
``delivery_uncertain``.

Fix: ``worker_id`` is mandatory, the previous state must be ``sending`` and the
caller must still own the lease; affected-row counts are checked by the caller.

Both defects are reproduced deterministically: concurrency is coordinated with a
``threading.Barrier`` inside the AI call, never with sleeps.
"""

from __future__ import annotations

import inspect
import threading
from datetime import datetime, timezone

import pytest

from kalampeach import repository
from kalampeach.ai.types import AIUnavailable
from kalampeach.services.capture import CaptureService
from kalampeach.services.worker import Worker
from conftest import (
    EMPLOYEE_ID,
    OTHER_EMPLOYEE_ID,
    clarification_answer,
)
from helpers import (
    bot_messages,
    clarifications,
    enqueue,
    fetch_one,
    private_update,
    process,
    revisions,
    run_worker,
    seed_pending_clarification,
    with_worker,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def status_of(ctx, clarification_id: int) -> str:
    with ctx.db.connection() as connection:
        return str(repository.get_clarification(connection, clarification_id)["status"])


def clarification_row(ctx, clarification_id: int):
    with ctx.db.connection() as connection:
        return repository.get_clarification(connection, clarification_id)


def deliver_question(ctx) -> int:
    """Run the worker so the seeded question is really sent; return its id."""

    Worker(ctx).run_once(now=ctx.now(), skip_reports=True)
    row = fetch_one(
        ctx,
        "SELECT message_id FROM bot_messages WHERE kind = 'clarification_prompt' ORDER BY message_id DESC",
    )
    assert row is not None
    return int(row["message_id"])


def clarification_revisions(ctx, event_id: int):
    return [rev for rev in revisions(ctx, event_id) if rev["change_type"] == "clarification"]


def source_row(ctx, message_id: int):
    return fetch_one(
        ctx,
        "SELECT processing_status, failure_reason FROM source_messages WHERE message_id = ?",
        (message_id,),
    )


def confirmation_count(ctx, event_id: int) -> int:
    return int(
        fetch_one(
            ctx,
            "SELECT COUNT(*) AS total FROM bot_messages"
            " WHERE kind = 'event_confirmation' AND event_id = ?",
            (event_id,),
        )["total"]
    )


def make_source(ctx, *, update_id: int, message_id: int, chat_id: int = EMPLOYEE_ID) -> int:
    """Persist a source message the way the processor does before routing."""

    now = ctx.now_iso()
    with ctx.db.transaction() as connection:
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=update_id,
            chat_id=chat_id,
            chat_type="private",
            message_id=message_id,
            reply_to_message_id=None,
            sender_telegram_user_id=chat_id,
            message_text="پاسخ",
            message_timestamp_utc=now,
            processing_status="received",
            now=now,
        )
    return int(source["id"])


def answer_message(*, message_id: int, text: str, sender_id: int = EMPLOYEE_ID):
    return type(
        "M",
        (),
        {
            "sender_id": sender_id,
            "text": text,
            "chat_id": sender_id,
            "message_id": message_id,
            "message_timestamp_utc": datetime(2026, 11, 23, 12, 5, tzinfo=timezone.utc),
        },
    )()


def set_clarification(
    ctx, clarification_id: int, *, status: str, lease_owner=None, question_count=None
):
    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE clarifications SET status = ?, lease_owner = ?, question_count = COALESCE(?, question_count)"
            " WHERE id = ?",
            (status, lease_owner, question_count, clarification_id),
        )


# ---------------------------------------------------------------------------
# defect 1 — one answer only
# ---------------------------------------------------------------------------


def test_two_concurrent_valid_answers_resolve_the_clarification_once(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """Two distinct answers race past the pre-check; exactly one may apply."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)

    first = private_update(update_id=1, message_id=201, text="۴۵ هزار تومان", reply_to=question)
    second = private_update(update_id=2, message_id=202, text="۵۰ هزار تومان", reply_to=question)
    enqueue(ctx, first)
    enqueue(ctx, second)

    amounts = {"۴۵ هزار تومان": "45000", "۵۰ هزار تومان": "50000"}

    # Both workers are inside the AI call — i.e. both have already passed
    # ``clarification_accepts_answer`` — before either may continue. Each answer
    # keeps its own amount, so the applied value identifies the winner.
    barrier = threading.Barrier(2, timeout=30)

    def synchronized_complete(request):
        barrier.wait()
        return clarification_answer(values={"amount": amounts[request.answer_text]})

    monkeypatch.setattr(fake_ai, "complete_clarification", synchronized_complete)

    errors = []

    def run(worker_id: str) -> None:
        try:
            Worker(with_worker(ctx, worker_id)).run_once(now=ctx.now(), skip_reports=True)
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("claim-worker-a",)),
        threading.Thread(target=run, args=("claim-worker-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert status_of(ctx, clarification_id) == "resolved"
    assert len(clarification_revisions(ctx, event_id)) == 1, "exactly one revision"
    assert confirmation_count(ctx, event_id) == 1, "exactly one confirmation"

    statuses = sorted(
        str(source_row(ctx, message_id)["processing_status"]) for message_id in (201, 202)
    )
    assert statuses == ["ignored", "processed"], "one answer applied, one rejected"
    losing = [
        message_id
        for message_id in (201, 202)
        if str(source_row(ctx, message_id)["processing_status"]) == "ignored"
    ]
    assert [source_row(ctx, message_id)["failure_reason"] for message_id in losing] == [
        "answer_claim_lost"
    ]

    event = fetch_one(ctx, "SELECT status, amount FROM events WHERE id = ?", (event_id,))
    assert event["status"] == "confirmed"
    # The event carries exactly the amount of the answer whose source was processed.
    winning = [
        message_id
        for message_id in (201, 202)
        if str(source_row(ctx, message_id)["processing_status"]) == "processed"
    ]
    winning_amount = "45000" if winning == [201] else "50000"
    assert int(event["amount"]) == int(winning_amount)


def test_losing_answer_changes_nothing_else(ctx, fake_ai, fake_telegram):
    """A late second answer after a resolution applies no revision and no message."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)

    stale_row = clarification_row(ctx, clarification_id)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    process(
        ctx,
        private_update(update_id=1, message_id=211, text="۴۵ هزار", reply_to=question),
        skip_reports=True,
    )
    assert status_of(ctx, clarification_id) == "resolved"
    revisions_before = len(clarification_revisions(ctx, event_id))
    confirmations_before = confirmation_count(ctx, event_id)
    messages_before = len(fake_telegram.sent)

    # A second, distinct answer whose state snapshot was read before the first one
    # applied — exactly what a second worker sees when it passes the pre-check.
    source_id = make_source(ctx, update_id=2, message_id=212)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "50000"}))
    outcome = CaptureService(ctx).handle_answer(
        message=answer_message(message_id=212, text="۵۰ هزار"),
        source_message_id=source_id,
        clarification=stale_row,
    )

    assert outcome.action == "duplicate"
    assert outcome.note == "answer_claim_lost"
    assert len(clarification_revisions(ctx, event_id)) == revisions_before
    assert confirmation_count(ctx, event_id) == confirmations_before
    assert len(fake_telegram.sent) == messages_before, "no extra message is sent"
    assert str(source_row(ctx, 212)["processing_status"]) == "ignored"
    assert str(source_row(ctx, 212)["failure_reason"]) == "answer_claim_lost"
    event = fetch_one(ctx, "SELECT amount FROM events WHERE id = ?", (event_id,))
    assert int(event["amount"]) == 45000


def test_claim_is_impossible_for_terminal_and_undelivered_states(ctx):
    """Only an answerable question can be claimed; closed rows stay closed."""

    now = ctx.now_iso()
    for index, state in enumerate(
        ("resolved", "expired", "unable_to_deliver", "pending_delivery"), start=1
    ):
        _, clarification_id = seed_pending_clarification(ctx, seq=900 + index, status=state)
        source_id = make_source(ctx, update_id=4700 + index, message_id=7000 + index)
        with ctx.db.transaction() as connection:
            claimed = repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=source_id,
                now=now,
            )
        assert claimed == 0, state
        assert status_of(ctx, clarification_id) == state


def test_claim_succeeds_for_every_answerable_state(ctx):
    now = ctx.now_iso()
    for index, state in enumerate(("awaiting_answer", "sending", "delivery_uncertain"), start=1):
        _, clarification_id = seed_pending_clarification(ctx, seq=950 + index, status=state)
        source_id = make_source(ctx, update_id=4800 + index, message_id=7100 + index)
        with ctx.db.transaction() as connection:
            claimed = repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=source_id,
                now=now,
            )
            row = repository.get_clarification(connection, clarification_id)
        assert claimed == 1, state
        assert str(row["status"]) == "answered"
        assert int(row["response_source_message_id"]) == source_id


def test_second_claim_for_another_answer_is_refused(ctx):
    """The database, not the caller, decides who owns the clarification."""

    _, clarification_id = seed_pending_clarification(ctx, status="awaiting_answer")
    now = ctx.now_iso()
    first_source = make_source(ctx, update_id=4901, message_id=7201)
    second_source = make_source(ctx, update_id=4902, message_id=7202)
    with ctx.db.transaction() as connection:
        assert (
            repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=first_source,
                now=now,
            )
            == 1
        )
    with ctx.db.transaction() as connection:
        assert (
            repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=second_source,
                now=now,
            )
            == 0
        )
    row = clarification_row(ctx, clarification_id)
    assert int(row["response_source_message_id"]) == first_source, "the first owner is untouched"


def test_a_claim_that_was_not_applied_can_be_released(ctx):
    """Documented recovery for a claim held by a caller that then gave up."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    deliver_question(ctx)
    first = make_source(ctx, update_id=4951, message_id=7211)
    second = make_source(ctx, update_id=4952, message_id=7212)
    now = ctx.now_iso()

    with ctx.db.transaction() as connection:
        assert (
            repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=first,
                now=now,
            )
            == 1
        )
    with ctx.db.transaction() as connection:
        # Only the owner of the claim may release it.
        assert (
            repository.release_clarification_answer_claim(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=second,
                now=now,
            )
            == 0
        )
        row = repository.get_clarification(connection, clarification_id)
        assert str(row["status"]) == "answered"

    with ctx.db.transaction() as connection:
        assert (
            repository.release_clarification_answer_claim(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=first,
                now=now,
            )
            == 1
        )
    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "awaiting_answer"
    assert row["response_source_message_id"] is None
    assert row["answered_at_utc"] is None

    with ctx.db.transaction() as connection:
        assert (
            repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=second,
                now=now,
            )
            == 1
        )


def test_duplicate_telegram_update_stays_idempotent(ctx, fake_ai, fake_telegram):
    """The same update delivered twice resolves the clarification exactly once."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)
    payload = private_update(update_id=7, message_id=221, text="۴۵ هزار", reply_to=question)

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    first = process(ctx, payload, skip_reports=True)
    # Same update_id again: the durable inbox refuses it before any AI call.
    second = process(ctx, payload, skip_reports=True)

    assert first is not None and second is not None
    assert status_of(ctx, clarification_id) == "resolved"
    assert len(clarification_revisions(ctx, event_id)) == 1
    assert confirmation_count(ctx, event_id) == 1
    assert len(fake_ai.calls) == 1, "the model is asked once"


def test_unauthorized_employee_cannot_claim_or_resolve(ctx, fake_ai, fake_telegram):
    """Ownership is checked before the claim, so a stranger cannot consume it."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)
    service = CaptureService(ctx)
    row = clarification_row(ctx, clarification_id)
    attempts_before = len(fake_telegram.attempts)

    message = type(
        "M",
        (),
        {
            "sender_id": OTHER_EMPLOYEE_ID,
            "text": "۴۵ هزار",
            "chat_id": OTHER_EMPLOYEE_ID,
            "message_timestamp_utc": ctx.now(),
            "message_id": 231,
        },
    )()
    outcome = service.handle_answer(
        message=message, source_message_id=150000, clarification=row
    )

    assert outcome.action == "ignored"
    assert outcome.note == "answer_not_owner"
    assert status_of(ctx, clarification_id) == "awaiting_answer"
    assert len(clarification_revisions(ctx, event_id)) == 0
    assert len(fake_telegram.attempts) == attempts_before, "the stranger's reply sends nothing"

    # The real owner can still claim it afterwards.
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    process(
        ctx,
        private_update(update_id=8, message_id=232, text="۴۵ هزار", reply_to=question),
        skip_reports=True,
    )
    assert status_of(ctx, clarification_id) == "resolved"


def test_ai_failure_before_claim_keeps_the_question_answerable(ctx, fake_ai):
    """A provider outage leaves the question open for the next attempt."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)

    fake_ai.clarification_answers.append(AIUnavailable("ai_down"))
    process(
        ctx,
        private_update(update_id=11, message_id=241, text="۴۵ هزار", reply_to=question),
        skip_reports=True,
    )
    assert status_of(ctx, clarification_id) == "awaiting_answer"
    assert len(clarification_revisions(ctx, event_id)) == 0

    ctx.clock.advance(seconds=600)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "resolved"
    assert len(clarification_revisions(ctx, event_id)) == 1


def test_failure_after_claim_rolls_back_and_the_answer_can_be_retried(
    ctx, fake_ai, monkeypatch
):
    """Claim + revision + resolution share one transaction: failure unlocks it."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    monkeypatch.setattr(repository, "insert_revision", explode)
    process(
        ctx,
        private_update(update_id=12, message_id=251, text="۴۵ هزار", reply_to=question),
        skip_reports=True,
    )

    # Nothing was applied and nothing is locked: the claim was rolled back.
    assert status_of(ctx, clarification_id) == "awaiting_answer"
    assert len(clarification_revisions(ctx, event_id)) == 0
    assert confirmation_count(ctx, event_id) == 0
    row = clarification_row(ctx, clarification_id)
    assert row["response_source_message_id"] is None, "no claim survived the rollback"

    monkeypatch.undo()
    ctx.clock.advance(seconds=600)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "resolved"
    assert len(clarification_revisions(ctx, event_id)) == 1
    assert confirmation_count(ctx, event_id) == 1


# ---------------------------------------------------------------------------
# defect 2 — uncertainty recovery is fenced and cannot rewrite a terminal row
# ---------------------------------------------------------------------------


def _uncertain(ctx, clarification_id: int, *, worker_id: str) -> int:
    with ctx.db.transaction() as connection:
        return repository.mark_clarification_delivery_uncertain(
            connection,
            clarification_id=clarification_id,
            error_category="bookkeeping_failed",
            now=ctx.now_iso(),
            worker_id=worker_id,
        )


@pytest.mark.parametrize("terminal_state", ["resolved", "answered", "expired", "unable_to_deliver"])
def test_uncertain_transition_is_rejected_for_terminal_states(ctx, terminal_state):
    _, clarification_id = seed_pending_clarification(ctx, status=terminal_state)

    assert _uncertain(ctx, clarification_id, worker_id=ctx.worker_id) == 0
    assert status_of(ctx, clarification_id) == terminal_state


def test_uncertain_transition_is_rejected_for_a_stale_worker(ctx):
    """A worker whose lease was taken over may not rewrite the new owner's row."""

    _, clarification_id = seed_pending_clarification(
        ctx, status="sending", lease_expires_at="2026-11-23T11:00:00.000Z"
    )
    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE clarifications SET lease_owner = ? WHERE id = ?",
            ("worker-b", clarification_id),
        )

    assert _uncertain(ctx, clarification_id, worker_id="worker-a") == 0
    assert status_of(ctx, clarification_id) == "sending"
    with ctx.db.connection() as connection:
        row = repository.get_clarification(connection, clarification_id)
    assert str(row["lease_owner"]) == "worker-b", "the takeover is preserved"


def test_uncertain_transition_still_applies_for_the_lease_owner(ctx):
    _, clarification_id = seed_pending_clarification(ctx, status="sending")
    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE clarifications SET lease_owner = ? WHERE id = ?",
            (ctx.worker_id, clarification_id),
        )

    assert _uncertain(ctx, clarification_id, worker_id=ctx.worker_id) == 1
    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "delivery_uncertain"
    assert row["lease_owner"] is None
    assert str(row["last_delivery_error"]) == "bookkeeping_failed"


def test_uncertain_transition_is_one_conditional_update():
    """The state and ownership filters must live inside the writing statement."""

    source = inspect.getsource(repository.mark_clarification_delivery_uncertain)
    upper = source.upper()
    assert "SELECT" not in upper, "no read-then-write"
    assert "STATUS = 'SENDING'" in upper.replace('"', "'")
    assert "LEASE_OWNER = ?" in upper
    assert "IS NULL OR" not in upper, "no fence bypass"


def test_recovery_after_a_resolution_preserves_resolved_and_sends_nothing(
    ctx, fake_ai, fake_telegram
):
    """The real recovery path must be a no-op once the answer already applied."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    process(
        ctx,
        private_update(update_id=21, message_id=261, text="۴۵ هزار", reply_to=question),
        skip_reports=True,
    )
    assert status_of(ctx, clarification_id) == "resolved"
    attempts_before = len(fake_telegram.attempts)
    confirmations_before = confirmation_count(ctx, event_id)

    CaptureService(ctx)._mark_uncertain_after_success(
        clarification_id, RuntimeError("database is locked")
    )

    assert status_of(ctx, clarification_id) == "resolved"
    assert len(fake_telegram.attempts) == attempts_before, "no message, no resend"
    assert confirmation_count(ctx, event_id) == confirmations_before


def test_bookkeeping_failure_after_a_confirmed_send_is_recorded_once_and_not_resent(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """A send whose bookkeeping failed becomes uncertain; it is never re-sent."""

    _, clarification_id = seed_pending_clarification(ctx)

    def explode(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repository, "record_bot_message", explode)
    run_worker(ctx, skip_reports=True)

    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "delivery_uncertain"
    assert str(row["last_delivery_error"]) == "bookkeeping_failed"
    attempts = len(fake_telegram.attempts)
    assert attempts == 1

    monkeypatch.undo()
    ctx.clock.advance(hours=1)
    run_worker(ctx, skip_reports=True)
    assert len(fake_telegram.attempts) == attempts, "an uncertain question is never resent"
    assert status_of(ctx, clarification_id) == "delivery_uncertain"


def test_recovery_path_cannot_overwrite_an_expired_or_undeliverable_row(
    ctx, fake_telegram
):
    service = CaptureService(ctx)
    for index, state in enumerate(("expired", "unable_to_deliver"), start=1):
        _, clarification_id = seed_pending_clarification(ctx, seq=800 + index, status=state)
        service._mark_uncertain_after_success(clarification_id, RuntimeError("boom"))
        assert status_of(ctx, clarification_id) == state


# ---------------------------------------------------------------------------
# lease-ownership cannot be bypassed by omitting the worker id
# ---------------------------------------------------------------------------


def _transition_calls(ctx, clarification_id: int):
    now = ctx.now_iso()
    return [
        (
            "mark_clarification_delivered",
            lambda connection: repository.mark_clarification_delivered(
                connection,
                clarification_id=clarification_id,
                now=now,
                question_count=1,
                worker_id=None,
            ),
        ),
        (
            "release_clarification_for_retry",
            lambda connection: repository.release_clarification_for_retry(
                connection,
                clarification_id=clarification_id,
                error_category="telegram_timeout",
                next_attempt_at=now,
                now=now,
                worker_id="",
            ),
        ),
        (
            "mark_clarification_undeliverable",
            lambda connection: repository.mark_clarification_undeliverable(
                connection,
                clarification_id=clarification_id,
                error_category="recipient_unavailable",
                now=now,
                worker_id=None,
            ),
        ),
        (
            "mark_clarification_delivery_uncertain",
            lambda connection: repository.mark_clarification_delivery_uncertain(
                connection,
                clarification_id=clarification_id,
                error_category="bookkeeping_failed",
                now=now,
                worker_id=None,
            ),
        ),
    ]


@pytest.mark.parametrize(
    "transition",
    [
        "mark_clarification_delivered",
        "release_clarification_for_retry",
        "mark_clarification_undeliverable",
        "mark_clarification_delivery_uncertain",
    ],
)
def test_clarification_transitions_require_a_worker_id(ctx, transition):
    _, clarification_id = seed_pending_clarification(ctx, status="sending")
    call = dict(_transition_calls(ctx, clarification_id))[transition]

    with pytest.raises(ValueError):
        with ctx.db.transaction() as connection:
            call(connection)

    assert status_of(ctx, clarification_id) == "sending", "nothing was written"


def test_report_transitions_require_a_worker_id(ctx):
    """The same ownership rule holds for report deliveries and inbox rows."""

    from helpers import seed_report_delivery

    delivery_id = int(seed_report_delivery(ctx, status="sending"))
    now = ctx.now_iso()
    calls = [
        lambda connection: repository.release_report_delivery(
            connection,
            delivery_id=delivery_id,
            status="failed",
            error_category="telegram_error",
            next_attempt_at=None,
            now=now,
            worker_id="",
        ),
        lambda connection: repository.update_report_progress(
            connection,
            delivery_id=delivery_id,
            chunk_count=2,
            chunks_sent=1,
            telegram_message_ids=[1],
            snapshot_json=None,
            narrative_used=False,
            now=now,
            worker_id=None,
        ),
        lambda connection: repository.mark_report_sent(
            connection, delivery_id=delivery_id, now=now, worker_id=""
        ),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            with ctx.db.transaction() as connection:
                call(connection)

    row = fetch_one(ctx, "SELECT status FROM report_deliveries WHERE id = ?", (delivery_id,))
    assert str(row["status"]) == "sending"

    with pytest.raises(TypeError):
        repository.finish_update(  # type: ignore[call-arg]
            object(), update_id=1, status="done", now=now
        )


def test_resolved_rows_are_never_rewritten_by_late_bookkeeping(ctx):
    """resolve/expire/requeue are filtered to non-terminal states in SQL."""

    _, clarification_id = seed_pending_clarification(ctx, status="answered")
    now = ctx.now_iso()
    with ctx.db.transaction() as connection:
        assert (
            repository.mark_clarification_resolved(
                connection, clarification_id=clarification_id, now=now
            )
            == 1
        )
    resolved_at = clarification_row(ctx, clarification_id)["resolved_at_utc"]

    ctx.clock.advance(minutes=5)
    with ctx.db.transaction() as connection:
        assert (
            repository.mark_clarification_resolved(
                connection, clarification_id=clarification_id, now=ctx.now_iso()
            )
            == 0
        )
        assert (
            repository.expire_clarification(
                connection, clarification_id=clarification_id, now=ctx.now_iso()
            )
            == 0
        )
        assert (
            repository.requeue_clarification_question(
                connection,
                clarification_id=clarification_id,
                question_text="مبلغ؟",
                missing_fields=["amount"],
                now=ctx.now_iso(),
            )
            == 0
        )

    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "resolved"
    assert row["resolved_at_utc"] == resolved_at


def test_answer_claim_rows_stay_consistent_after_a_partial_answer(ctx, fake_ai):
    """An incomplete answer requeues the question and frees the claim again."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    question = deliver_question(ctx)

    fake_ai.clarification_answers.append(
        clarification_answer(values={"amount": None}, missing_fields=("amount",))
    )
    process(
        ctx,
        private_update(update_id=31, message_id=271, text="نمی‌دانم", reply_to=question),
        skip_reports=True,
    )

    row = clarification_row(ctx, clarification_id)
    # The requeue and the next delivery can happen in the same worker run.
    assert str(row["status"]) in ("pending_delivery", "awaiting_answer", "sending")
    assert row["response_source_message_id"] is None, "the old claim was released"
    assert int(row["question_count"]) >= 1
    assert clarification_revisions(ctx, event_id) == []
    assert confirmation_count(ctx, event_id) == 0

    # The requeued question is delivered, then answered for real.
    ctx.clock.advance(seconds=60)
    run_worker(ctx, skip_reports=True)
    second_question = fetch_one(
        ctx,
        "SELECT message_id FROM bot_messages WHERE kind = 'clarification_prompt' ORDER BY message_id DESC",
    )
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    process(
        ctx,
        private_update(
            update_id=32,
            message_id=272,
            text="۴۵ هزار",
            reply_to=int(second_question["message_id"]),
        ),
        skip_reports=True,
    )
    assert status_of(ctx, clarification_id) == "resolved"
    assert len(clarification_revisions(ctx, event_id)) == 1
