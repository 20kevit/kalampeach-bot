"""Regression tests for the immediate clarification-reply race (defect A6).

Reproduced defect at commit 234d134
------------------------------------
``CaptureService._deliver_one`` sent the question through Telegram and only then
wrote the two rows that record it (``clarifications.status = 'awaiting_answer'`` and
the ``bot_messages`` mapping that maps the Telegram message id to the clarification).
``MessageProcessor._resolve_reply_target`` needs that mapping, so an employee who
answered immediately — or an employee whose sending worker died before the mapping
was written — had the answer discarded:

* the reply source row became ``ignored`` / ``unresolved_reply_target``,
* the inbox row became ``done`` (so it was never retried),
* the employee received the generic "can't resolve your reply" notice,
* the event stayed ``pending_clarification`` forever, even after the mapping was
  finally committed.

The window is simulated deterministically (no sleeps): the two bookkeeping writes are
captured instead of applied, replayed later by :class:`DeferredSendBookkeeping`.

Fix under test
--------------
``sending`` and ``delivery_uncertain`` are durable proof that the bot tried to send a
question to that employee without being able to record which message it became. A reply
whose quoted message is unknown is therefore attributed to that question when there is
exactly one candidate for that numeric sender, and ``clarification_accepts_answer``
accepts those states. The sending worker's own transition stays fenced by
``status = 'sending' AND lease_owner``, so the answer wins and the delivery state is not
re-applied.
"""

from __future__ import annotations

import pytest

from kalampeach import repository
from kalampeach.services import messages
from kalampeach.services.capture import clarification_accepts_answer, CaptureService
from kalampeach.telegram.client import TelegramError
from kalampeach.timeutil import to_utc_iso
from conftest import EMPLOYEE_ID, GROUP_ID, OTHER_EMPLOYEE_ID, clarification_answer
from helpers import (
    bot_messages,
    clarifications,
    fetch_one,
    private_update,
    process,
    revisions,
    run_worker,
    seed_pending_clarification,
)


class DeferredSendBookkeeping:
    """Hold back the writes that record a successful send.

    Mirrors the real window between "Telegram accepted the question" and "the local
    rows are committed": the transition and the message-id mapping are captured
    instead of written, so the database shows exactly what an immediate reply sees.
    """

    def __init__(self, monkeypatch):
        self.writes = []
        self._mark = repository.mark_clarification_delivered
        self._record = repository.record_bot_message
        captured = self.writes

        def deferred_mark(connection, **kwargs):
            captured.append((self._mark, kwargs))
            return 1  # the sender believes the transition succeeded

        def deferred_record(connection, **kwargs):
            captured.append((self._record, kwargs))

        monkeypatch.setattr(repository, "mark_clarification_delivered", deferred_mark)
        monkeypatch.setattr(repository, "record_bot_message", deferred_record)

    def commit(self) -> None:
        """The sender's bookkeeping finally commits (possibly much later)."""

        for function, kwargs in self.writes:
            with _transaction_ctx() as connection:
                function(connection, **kwargs)
        self.writes.clear()


_TRANSACTION_CTX = {"factory": None}


def _transaction_ctx():
    factory = _TRANSACTION_CTX["factory"]
    assert factory is not None, "bind_window_ctx() must be called first"
    return factory()


def bind_window_ctx(ctx) -> None:
    _TRANSACTION_CTX["factory"] = ctx.db.transaction


def status_of(ctx, clarification_id: int) -> str:
    return str(
        fetch_one(ctx, "SELECT status FROM clarifications WHERE id = ?", (clarification_id,))["status"]
    )


def event_of(ctx, event_id: int):
    return fetch_one(ctx, "SELECT * FROM events WHERE id = ?", (event_id,))


def question_message_id(fake_telegram) -> int:
    """The Telegram id of the one question the bot sent."""

    sent = [item for item in fake_telegram.sent if item["chat_id"] == EMPLOYEE_ID]
    assert len(sent) == 1, sent
    return int(sent[0]["message_id"])


def reply_about(ctx, *, update_id: int, message_id: int, target: int, sender_id=EMPLOYEE_ID,
                chat_id=None, text: str = "۴۵ هزار تومان"):
    payload = private_update(
        update_id=update_id,
        message_id=message_id,
        text=text,
        reply_to=target,
        sender_id=sender_id,
        chat_id=chat_id if chat_id is not None else sender_id,
    )
    return process(ctx, payload, skip_reports=True)


def source_row(ctx, message_id: int):
    return fetch_one(
        ctx, "SELECT processing_status, failure_reason FROM source_messages WHERE message_id = ?",
        (message_id,),
    )


# ---------------------------------------------------------------------------
# 1-2. The reply arrives before the mapping is persisted.
# ---------------------------------------------------------------------------
def test_immediate_reply_is_answered_while_the_mapping_is_missing(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)

    summary = run_worker(ctx, skip_reports=True)
    assert summary["clarifications"]["sent"] == 1

    question = question_message_id(fake_telegram)
    assert bot_messages(ctx) == [], "the message-id mapping is not persisted yet"
    assert status_of(ctx, clarification_id) == "sending"

    # The employee answers immediately, quoting the question they just received.
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    reply_about(ctx, update_id=1, message_id=77, target=question)

    event = event_of(ctx, event_id)
    assert event["status"] == "confirmed"
    assert event["amount"] == "45000"
    assert status_of(ctx, clarification_id) == "resolved"
    assert source_row(ctx, 77)["processing_status"] == "processed"
    confirmations = [
        text for text in fake_telegram.texts_to(EMPLOYEE_ID) if text.startswith("✅ ثبت شد")
    ]
    assert len(confirmations) == 1
    assert messages.REPLY_TARGET_UNRESOLVED not in fake_telegram.all_text()

    # The sender's bookkeeping lands afterwards: it cannot reopen or re-apply anything.
    monkeypatch.undo()
    window.commit()

    assert status_of(ctx, clarification_id) == "resolved"
    assert event_of(ctx, event_id)["status"] == "confirmed"
    assert len(bot_messages(ctx, "clarification_prompt")) == 1, (
        "the real message id is recorded once it is known"
    )
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]


def test_reply_that_arrives_after_the_mapping_is_applied_once(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """The same answer is not applied twice when the mapping arrives later."""

    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    reply_about(ctx, update_id=1, message_id=77, target=question)
    assert event_of(ctx, event_id)["status"] == "confirmed"

    monkeypatch.undo()
    window.commit()

    # A second message quoting the same (now mapped) question must not re-apply it.
    # No AI answer is queued: an unexpected AI call would fail the test.
    second = reply_about(ctx, update_id=2, message_id=78, target=question, text="۴۵ هزار تومان")
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]
    assert status_of(ctx, clarification_id) == "resolved"
    assert second is not None  # the run completed; the answer was simply not re-applied


# ---------------------------------------------------------------------------
# 3. A process interruption during the sequence does not lose the answer.
# ---------------------------------------------------------------------------
def test_sending_process_killed_before_the_mapping_still_accepts_the_answer(
    ctx, fake_ai, fake_telegram
):
    """Telegram accepted the question, the sender died: no mapping will ever exist."""

    event_id, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("telegram_timeout", uncertain=True))

    run_worker(ctx, skip_reports=True)
    assert status_of(ctx, clarification_id) == "delivery_uncertain"
    assert bot_messages(ctx) == []

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "8000"}))
    reply_about(ctx, update_id=3, message_id=81, target=5001)  # id the "lost" send used

    assert event_of(ctx, event_id)["status"] == "confirmed"
    assert event_of(ctx, event_id)["amount"] == "8000"
    assert status_of(ctx, clarification_id) == "resolved"

    # And no blind resend happened at any point: exactly one question attempt.
    ctx.clock.advance(hours=1)
    run_worker(ctx, skip_reports=True)
    row = fetch_one(ctx, "SELECT * FROM clarifications WHERE id = ?", (clarification_id,))
    assert row["delivery_attempts"] == 1
    question_attempts = [
        item
        for item in fake_telegram.attempts
        if item["chat_id"] == EMPLOYEE_ID and "؟" in item["text"]
    ]
    assert len(question_attempts) == 1


def test_processor_interruption_retries_the_answer_until_it_lands(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """A crash while applying the answer leaves the reply queued, not lost."""

    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)

    real_insert = repository.insert_revision
    calls = {"count": 0}

    def flaky_insert(connection, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("process killed while applying the answer")
        return real_insert(connection, **kwargs)

    monkeypatch.setattr(repository, "insert_revision", flaky_insert)
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "12000"}))
    summary = reply_about(ctx, update_id=4, message_id=91, target=question)
    assert summary["retried"] == 1
    assert event_of(ctx, event_id)["status"] == "pending_clarification", "no partial write"
    assert status_of(ctx, clarification_id) == "sending", "the answer did not half-apply"
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created"]

    ctx.clock.advance(seconds=120)  # the queued retry becomes due
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "12000"}))
    monkeypatch.undo()
    window.commit()
    run_worker(ctx, skip_reports=True)

    assert event_of(ctx, event_id)["status"] == "confirmed"
    assert event_of(ctx, event_id)["amount"] == "12000"
    assert status_of(ctx, clarification_id) == "resolved"
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]


# ---------------------------------------------------------------------------
# 4. Idempotency of duplicated Telegram updates.
# ---------------------------------------------------------------------------
def test_duplicate_update_does_not_apply_the_answer_twice(ctx, fake_ai, fake_telegram):
    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    run_worker(ctx, skip_reports=True)  # normal path: the mapping exists
    question = question_message_id(fake_telegram)
    assert len(bot_messages(ctx, "clarification_prompt")) == 1

    payload = private_update(
        update_id=5, message_id=101, text="۴۵ هزار تومان", reply_to=question
    )
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    process(ctx, payload, skip_reports=True)
    assert event_of(ctx, event_id)["status"] == "confirmed"

    # Same update_id delivered again by Telegram: acknowledged as a duplicate.
    with ctx.db.transaction() as connection:
        assert (
            repository.enqueue_update(
                connection,
                update_id=5,
                raw_payload=__import__("json").dumps(payload, ensure_ascii=False),
                chat_id=EMPLOYEE_ID,
                chat_type="private",
                now=to_utc_iso(ctx.now()),
            )
            is False
        )
    run_worker(ctx, skip_reports=True)
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]

    # A different update carrying the same message body is not re-applied either:
    # the clarification is already resolved, and no AI answer is queued.
    process(
        ctx,
        private_update(update_id=6, message_id=102, text="۴۵ هزار تومان", reply_to=question),
        skip_reports=True,
    )
    assert [rev["change_type"] for rev in revisions(ctx, event_id)] == ["created", "clarification"]
    assert event_of(ctx, event_id)["amount"] == "45000"


# ---------------------------------------------------------------------------
# 5. No cross-employee answers, no guessing.
# ---------------------------------------------------------------------------
def test_another_employee_cannot_answer_someone_elses_question(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)

    # Another authorized employee replies, quoting the same message id, in their
    # own private chat. The attribution query is keyed by the numeric sender id,
    # so nothing of the first employee's is reachable.
    other = reply_about(
        ctx, update_id=7, message_id=111, target=question, sender_id=OTHER_EMPLOYEE_ID
    )
    assert other["done"] == 1
    assert event_of(ctx, event_id)["status"] == "pending_clarification"
    assert status_of(ctx, clarification_id) == "sending"
    assert fake_ai.calls == [] or "complete_clarification" not in fake_ai.calls
    assert source_row(ctx, 111)["failure_reason"] == "unresolved_reply_target"
    assert messages.REPLY_TARGET_UNRESOLVED in fake_telegram.texts_to(OTHER_EMPLOYEE_ID)

    # The owner can still answer afterwards.
    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    reply_about(ctx, update_id=8, message_id=112, target=question)
    assert event_of(ctx, event_id)["status"] == "confirmed"
    assert status_of(ctx, clarification_id) == "resolved"


def test_ambiguous_unconfirmed_questions_are_not_guessed(ctx, fake_ai, fake_telegram):
    seed_pending_clarification(ctx, seq=1, status="delivery_uncertain")
    seed_pending_clarification(ctx, seq=2, status="delivery_uncertain")

    summary = reply_about(ctx, update_id=9, message_id=121, target=5555)

    assert summary["done"] == 1
    assert [row["status"] for row in clarifications(ctx)] == [
        "delivery_uncertain",
        "delivery_uncertain",
    ]
    statuses = [
        fetch_one(ctx, "SELECT status FROM events WHERE id = ?", (index,))["status"]
        for index in (1, 2)
    ]
    assert statuses == ["pending_clarification", "pending_clarification"]
    assert "complete_clarification" not in fake_ai.calls
    assert source_row(ctx, 121)["failure_reason"] == "unresolved_reply_target"
    assert messages.REPLY_TARGET_UNRESOLVED in fake_telegram.texts_to(EMPLOYEE_ID)


def test_expired_or_unattempted_questions_are_not_reopened(ctx, fake_ai, fake_telegram):
    event_id, clarification_id = seed_pending_clarification(ctx, status="expired")

    reply_about(ctx, update_id=10, message_id=131, target=6666)

    assert status_of(ctx, clarification_id) == "expired"
    assert event_of(ctx, event_id)["status"] == "pending_clarification"
    assert "complete_clarification" not in fake_ai.calls
    assert source_row(ctx, 131)["failure_reason"] == "unresolved_reply_target"


# ---------------------------------------------------------------------------
# 6. The state matrix behind the fix.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, accepted",
    [
        ("awaiting_answer", True),
        ("sending", True),
        ("delivery_uncertain", True),
        ("pending_delivery", False),
        ("answered", False),
        ("resolved", False),
        ("expired", False),
        ("unable_to_deliver", False),
    ],
)
def test_answer_acceptance_matrix(ctx, status, accepted):
    event_id, clarification_id = seed_pending_clarification(ctx, status=status)
    row = fetch_one(ctx, "SELECT * FROM clarifications WHERE id = ?", (clarification_id,))
    assert clarification_accepts_answer(row, ctx.now_iso()) is accepted


def test_live_sending_claim_accepts_the_answer_and_the_sender_yields(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """The answer wins; the sender records the mapping but does not re-apply state."""

    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "30000"}))
    reply_about(ctx, update_id=11, message_id=141, target=question)
    assert status_of(ctx, clarification_id) == "resolved"

    monkeypatch.undo()
    window.commit()  # the fenced transition changes nothing; the mapping is recorded
    row = fetch_one(ctx, "SELECT * FROM clarifications WHERE id = ?", (clarification_id,))
    assert row["status"] == "resolved"
    assert row["question_count"] == 0, "the sender must not claim it asked a resolved question"
    assert len(bot_messages(ctx, "clarification_prompt")) == 1


def test_the_group_never_receives_business_details_from_the_race(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)

    fake_ai.clarification_answers.append(clarification_answer(values={"amount": "45000"}))
    reply_about(ctx, update_id=12, message_id=151, target=question)

    assert fake_telegram.texts_to(GROUP_ID) == []
    assert CaptureService(ctx).ctx.worker_id  # service still constructible after the race


def test_group_reply_is_never_attributed_to_a_private_question(
    ctx, fake_ai, fake_telegram, monkeypatch
):
    """Only a private reply from the owner may be attributed to a question."""

    bind_window_ctx(ctx)
    event_id, clarification_id = seed_pending_clarification(ctx)
    window = DeferredSendBookkeeping(monkeypatch)
    run_worker(ctx, skip_reports=True)
    question = question_message_id(fake_telegram)
    assert status_of(ctx, clarification_id) == "sending"

    # The same employee replies in the group to a message the bot does not know.
    group_reply = __import__("helpers").telegram_update(
        update_id=20, message_id=201, text="۴۵ هزار تومان", reply_to=question
    )
    process(ctx, group_reply, skip_reports=True)

    assert status_of(ctx, clarification_id) == "sending"
    assert event_of(ctx, event_id)["status"] == "pending_clarification"
    assert "complete_clarification" not in fake_ai.calls
    assert fake_telegram.texts_to(GROUP_ID) == []
