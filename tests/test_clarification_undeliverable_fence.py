"""Regression tests for the setup-hint side effect of a rejected permanent send.

Defect at e504abd
-----------------
``CaptureService._handle_send_failure`` called ``mark_clarification_undeliverable``
and then ``_send_setup_hint`` **without looking at the returned row count**. The
transition is fenced by ``status = 'sending' AND lease_owner = <worker>``, so a
worker whose row had already moved on — taken over by another run, swept to
``delivery_uncertain`` after its lease expired, or answered/resolved — changed
zero rows but still posted the neutral "start the bot" hint into the group. That
is an out-of-band message caused by a transition the database had just rejected.

Fix under test
--------------
The permanent-failure path reads the affected-row count and only announces a
setup hint when exactly one eligible row was changed. A rejection sends nothing,
retries nothing and mutates no state (the run summary reports ``rejected``).

Everything here is deterministic: the "takeover" and the "settled meanwhile"
cases are produced by ordinary repository calls (or one lease transfer) inside
the recording Telegram call, never by sleeps or threads.
"""

from __future__ import annotations

from datetime import timedelta

from kalampeach import repository
from kalampeach.services import messages
from kalampeach.services.capture import CaptureService
from kalampeach.telegram.client import TelegramError
from kalampeach.timeutil import to_utc_iso
from conftest import GROUP_ID
from helpers import clarifications, seed_pending_clarification, with_worker


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def clarification_row(ctx, clarification_id: int):
    with ctx.db.connection() as connection:
        return repository.get_clarification(connection, clarification_id)


def transfer_lease(ctx, clarification_id: int, worker_id: str) -> None:
    """Hand the delivery to another worker, as a takeover leaves it behind."""

    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE clarifications SET lease_owner = ? WHERE id = ?",
            (worker_id, clarification_id),
        )


def group_texts(fake_telegram):
    return fake_telegram.texts_to(GROUP_ID)


# ---------------------------------------------------------------------------
# Test A — a stale worker must not announce anything
# ---------------------------------------------------------------------------


def test_stale_worker_after_takeover_sends_no_setup_hint(ctx, fake_telegram, monkeypatch):
    """Worker A loses the lease; its permanent failure is rejected silently."""

    _, clarification_id = seed_pending_clarification(ctx)
    seeded = clarification_row(ctx, clarification_id)
    service_a = CaptureService(with_worker(ctx, "worker-a"))
    attempts = []

    def send_with_takeover(chat_id, text, **kwargs):
        # Worker B takes over the delivery while A is still inside its Telegram
        # call; A's failure then refers to a row it no longer owns.
        attempts.append((int(chat_id), text))
        transfer_lease(ctx, clarification_id, "worker-b")
        raise TelegramError("recipient_unavailable")

    monkeypatch.setattr(fake_telegram, "send_message", send_with_takeover)

    stats = service_a.deliver_pending_clarifications()

    assert stats.get("rejected", 0) == 1
    assert stats.get("unable", 0) == 0
    assert len(attempts) == 1, "A really did attempt the question once"
    assert fake_telegram.sent == [], "a rejected transition must send nothing"
    assert group_texts(fake_telegram) == [], "no setup hint"

    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "sending", "B's state is untouched"
    assert str(row["lease_owner"]) == "worker-b"
    assert int(row["delivery_attempts"]) == 1, "A did not retry the permanent failure"
    assert row["last_delivery_error"] is None, "no unfenced state mutation"
    assert row["next_delivery_attempt_at_utc"] == seeded["next_delivery_attempt_at_utc"]


def test_question_settled_while_sending_is_not_announced(ctx, fake_telegram, monkeypatch):
    """A sweep during A's slow send settles the row; A must stay silent."""

    _, clarification_id = seed_pending_clarification(ctx)
    service_a = CaptureService(with_worker(ctx, "worker-a"))

    def send_after_lease_expiry(chat_id, text, **kwargs):
        # Another cron run sweeps A's expired lease while A is still sending.
        later = to_utc_iso(ctx.now() + timedelta(hours=1))
        with ctx.db.transaction() as connection:
            swept = repository.expire_stale_clarification_deliveries(connection, now=later)
        assert swept == [clarification_id]
        raise TelegramError("recipient_unavailable")

    monkeypatch.setattr(fake_telegram, "send_message", send_after_lease_expiry)

    stats = service_a.deliver_pending_clarifications()

    assert stats.get("rejected", 0) == 1
    assert stats.get("unable", 0) == 0
    assert fake_telegram.sent == []
    assert group_texts(fake_telegram) == []
    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "delivery_uncertain", "the sweep's outcome is preserved"
    assert str(row["last_delivery_error"]) == "lease_expired_during_send"


# ---------------------------------------------------------------------------
# Test B — the current owner keeps the existing behaviour
# ---------------------------------------------------------------------------


def test_current_owner_sends_the_setup_hint_exactly_once(ctx, fake_telegram):
    _, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("recipient_unavailable"))

    stats = CaptureService(ctx).deliver_pending_clarifications()

    assert stats["unable"] == 1
    assert stats.get("rejected", 0) == 0
    row = clarification_row(ctx, clarification_id)
    assert str(row["status"]) == "unable_to_deliver"
    assert str(row["last_delivery_error"]) == "recipient_unavailable"
    assert row["lease_owner"] is None
    assert group_texts(fake_telegram) == [messages.GROUP_SETUP_HINT], "sent once"
    assert len(fake_telegram.attempts) == 2, "one question attempt plus one hint"


# ---------------------------------------------------------------------------
# Test C — repeating the failure path cannot repeat the message
# ---------------------------------------------------------------------------


def test_repeated_failure_path_sends_no_second_hint(ctx, fake_telegram):
    """Once the row is terminal, the same failure path must do nothing at all."""

    _, clarification_id = seed_pending_clarification(ctx)
    fake_telegram.queue_failure(TelegramError("recipient_unavailable"))
    CaptureService(ctx).deliver_pending_clarifications()

    row = clarification_row(ctx, clarification_id)
    attempts_before = len(fake_telegram.attempts)
    assert str(row["status"]) == "unable_to_deliver"
    assert group_texts(fake_telegram) == [messages.GROUP_SETUP_HINT]

    outcome = CaptureService(ctx)._handle_send_failure(
        row,
        TelegramError("recipient_unavailable"),
        ctx.now(),
        claimed_attempts=int(row["delivery_attempts"]),
    )

    assert outcome == "rejected"
    assert len(fake_telegram.attempts) == attempts_before, "no additional message"
    assert group_texts(fake_telegram) == [messages.GROUP_SETUP_HINT], "no duplicate hint"
    after = clarification_row(ctx, clarification_id)
    assert str(after["status"]) == "unable_to_deliver"
    assert int(after["delivery_attempts"]) == int(row["delivery_attempts"])
    assert after["next_delivery_attempt_at_utc"] is None, "a permanent failure is not retried"
    assert len(clarifications(ctx)) == 1


def test_one_stale_and_one_current_failure_produce_exactly_one_hint(ctx, fake_telegram):
    """A rejected failure and an accepted one in the same run: one hint, at most."""

    _, first_id = seed_pending_clarification(ctx, seq=1)
    _, second_id = seed_pending_clarification(ctx, seq=2)
    service = CaptureService(ctx)
    now = to_utc_iso(ctx.now())

    # First question: this worker claimed it, then the lease moved to worker-b.
    with ctx.db.transaction() as connection:
        assert repository.claim_clarification_for_delivery(
            connection,
            clarification_id=first_id,
            now=now,
            lease_seconds=60,
            worker_id=ctx.worker_id,
        )
        first_row = repository.get_clarification(connection, first_id)
    transfer_lease(ctx, first_id, "worker-b")
    assert (
        service._handle_send_failure(
            first_row, TelegramError("recipient_unavailable"), ctx.now(), claimed_attempts=1
        )
        == "rejected"
    )

    # Second question: still owned by this worker, so the existing path applies.
    with ctx.db.transaction() as connection:
        assert repository.claim_clarification_for_delivery(
            connection,
            clarification_id=second_id,
            now=now,
            lease_seconds=60,
            worker_id=ctx.worker_id,
        )
        second_row = repository.get_clarification(connection, second_id)
    assert (
        service._handle_send_failure(
            second_row, TelegramError("recipient_unavailable"), ctx.now(), claimed_attempts=1
        )
        == "unable"
    )

    assert group_texts(fake_telegram) == [messages.GROUP_SETUP_HINT], "exactly one hint"
    first = clarification_row(ctx, first_id)
    assert str(first["status"]) == "sending"
    assert str(first["lease_owner"]) == "worker-b"
    second = clarification_row(ctx, second_id)
    assert str(second["status"]) == "unable_to_deliver"
