"""Regression tests for report-delivery lease fencing (defect: unfenced transitions).

Reproduced defect at commit 234d134
------------------------------------
``claim_report_delivery`` recorded ``lease_owner``, but ``update_report_progress``,
``mark_report_sent`` and ``release_report_delivery`` had no ownership condition, so a
worker whose lease had expired could keep writing after another worker had taken the
delivery over. Concretely (executed against 234d134):

* A claims, A's lease expires, B claims;
* A's stale ``update_report_progress`` rewrites ``chunks_sent`` from 1 to 9 and stores a
  corrupted snapshot flag;
* A's stale ``mark_report_sent`` flips the row to ``sent`` for a report B is still
  sending;
* A's stale ``release_report_delivery`` clears B's lease and returns the row to
  ``pending``.

Every transition is now fenced by the lease owner in the same SQL statement, and every
caller checks the affected-row count.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalampeach import repository
from kalampeach.services import messages
from kalampeach.services.reports import DeliveryLeaseLost, ReportService
from kalampeach.timeutil import to_utc_iso
from conftest import MANAGER_ID
from helpers import fetch_one, report_deliveries, seed_report_delivery, with_worker

def lease_seconds(ctx) -> int:
    """The lease a delivery actually gets from configuration."""

    return int(ctx.settings.worker_lease_seconds)


def expire_lease(ctx) -> None:
    """Move the clock past the lease the current owner was granted."""

    ctx.clock.advance(seconds=lease_seconds(ctx) + 5)


def at(ctx, iso: str) -> None:
    ctx.clock.set(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def row_of(ctx, delivery_id: int):
    return fetch_one(ctx, "SELECT * FROM report_deliveries WHERE id = ?", (delivery_id,))


def snapshot(ctx, delivery_id: int) -> dict:
    row = row_of(ctx, delivery_id)
    return {
        "status": row["status"],
        "chunks_sent": row["chunks_sent"],
        "chunk_count": row["chunk_count"],
        "message_ids": row["telegram_message_ids"],
        "lease_owner": row["lease_owner"],
        "lease_expiry": row["lease_expires_at_utc"],
        "attempts": row["attempts"],
        "narrative_used": row["narrative_used"],
        "error": row["last_error_category"],
    }


def take_over(ctx, delivery_id: int, worker_id: str = "worker-b") -> None:
    """B claims the delivery after A's lease expired."""
    with ctx.db.transaction() as connection:
        claimed = repository.claim_report_delivery(
            connection,
            delivery_id=delivery_id,
            now=ctx.now_iso(),
            lease_seconds=lease_seconds(ctx),
            worker_id=worker_id,
        )
    assert claimed, "the second worker must be able to take over an expired lease"


def claim_as_a(ctx, delivery_id: int, worker_id: str = "worker-a") -> None:
    with ctx.db.transaction() as connection:
        claimed = repository.claim_report_delivery(
            connection,
            delivery_id=delivery_id,
            now=ctx.now_iso(),
            lease_seconds=lease_seconds(ctx),
            worker_id=worker_id,
        )
    assert claimed


def test_stale_progress_write_is_rejected(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=0)
    claim_as_a(ctx, delivery_id)

    expire_lease(ctx)  # A's lease ran out
    take_over(ctx, delivery_id)
    before = snapshot(ctx, delivery_id)

    with ctx.db.transaction() as connection:
        changed = repository.update_report_progress(
            connection,
            delivery_id=delivery_id,
            chunk_count=9,
            chunks_sent=9,
            telegram_message_ids=[9999],
            snapshot_json='{"text": "corrupted"}',
            narrative_used=True,
            now=ctx.now_iso(),
            worker_id="worker-a",
        )

    assert changed == 0, "a worker that lost the lease must not write progress"
    assert snapshot(ctx, delivery_id) == before
    assert row_of(ctx, delivery_id)["lease_owner"] == "worker-b"


def test_stale_mark_sent_is_rejected(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=1)
    claim_as_a(ctx, delivery_id)
    expire_lease(ctx)
    take_over(ctx, delivery_id)
    before = snapshot(ctx, delivery_id)

    with ctx.db.transaction() as connection:
        changed = repository.mark_report_sent(
            connection, delivery_id=delivery_id, now=ctx.now_iso(), worker_id="worker-a"
        )

    assert changed == 0
    assert row_of(ctx, delivery_id)["status"] == "sending"
    assert snapshot(ctx, delivery_id) == before


def test_stale_release_is_rejected_and_keeps_the_new_lease(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=1)
    claim_as_a(ctx, delivery_id)
    expire_lease(ctx)
    take_over(ctx, delivery_id)

    with ctx.db.transaction() as connection:
        changed = repository.release_report_delivery(
            connection,
            delivery_id=delivery_id,
            status="pending",
            error_category="stale_worker",
            next_attempt_at=None,
            now=ctx.now_iso(),
            worker_id="worker-a",
        )

    assert changed == 0
    row = row_of(ctx, delivery_id)
    assert row["status"] == "sending"
    assert row["lease_owner"] == "worker-b"
    assert row["lease_expires_at_utc"] is not None
    assert row["last_error_category"] != "stale_worker"


def test_stale_settlement_cannot_touch_a_live_takeover(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=1)
    claim_as_a(ctx, delivery_id)
    expire_lease(ctx)
    take_over(ctx, delivery_id)
    before = snapshot(ctx, delivery_id)

    with ctx.db.transaction() as connection:
        for outcome in (
            repository.STALE_SENT,
            repository.STALE_RESTART,
            repository.STALE_UNCERTAIN,
        ):
            assert (
                repository.settle_stale_report_delivery(
                    connection, delivery_id=delivery_id, now=ctx.now_iso(), outcome=outcome
                )
                == 0
            )

    assert snapshot(ctx, delivery_id) == before


def test_settlement_works_once_the_lease_is_really_gone(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=3)
    claim_as_a(ctx, delivery_id)
    ctx.clock.advance(minutes=5)  # lease expired, nobody took over

    with ctx.db.transaction() as connection:
        assert (
            repository.settle_stale_report_delivery(
                connection,
                delivery_id=delivery_id,
                now=ctx.now_iso(),
                outcome=repository.STALE_SENT,
            )
            == 1
        )
    assert row_of(ctx, delivery_id)["status"] == "sent"


def test_exhaustion_decision_cannot_override_a_live_claim(ctx):
    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=3, chunks_sent=0, attempts=3)
    claim_as_a(ctx, delivery_id)

    with ctx.db.transaction() as connection:
        settled = repository.fail_exhausted_report_delivery(
            connection, delivery_id=delivery_id, now=ctx.now_iso()
        )

    assert settled == 0
    row = row_of(ctx, delivery_id)
    assert row["status"] == "sending"
    assert row["lease_owner"] == "worker-a"


def test_owner_can_finish_after_taking_over(ctx, fake_telegram):
    """B takes over a delivery A abandoned before sending and finishes it."""

    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=0, chunks_sent=0)
    claim_as_a(ctx, delivery_id)  # A claims ...
    expire_lease(ctx)  # ... and dies without writing anything

    outcome = ReportService(with_worker(ctx, "worker-b"))._deliver(delivery_id, ctx.now())

    assert outcome == "sent"
    row = row_of(ctx, delivery_id)
    assert row["status"] == "sent"
    assert row["chunks_sent"] == row["chunk_count"]
    assert row["lease_owner"] is None
    assert len(fake_telegram.texts_to(MANAGER_ID)) == row["chunk_count"]


def test_stale_worker_stops_before_freezing_the_snapshot(ctx, fake_telegram, monkeypatch):
    """A claims, loses the lease before its first owned write, and stops."""

    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=0, chunks_sent=0, snapshot=False)

    real_load_or_build = ReportService._load_or_build
    state = {"done": False, "b_outcome": None}

    def load_after_takeover(self, row, moment):
        if not state["done"]:
            state["done"] = True
            # A's lease expires while it computes, and B runs a full delivery
            # attempt for the same row before A writes anything.
            expire_lease(ctx)
            state["b_outcome"] = ReportService(
                with_worker(ctx, "worker-b")
            )._deliver(delivery_id, ctx.now())
        return real_load_or_build(self, row, moment)

    monkeypatch.setattr(ReportService, "_load_or_build", load_after_takeover)
    outcome = ReportService(with_worker(ctx, "worker-a"))._deliver(delivery_id, ctx.now())

    assert outcome == "lease_lost"
    assert state["b_outcome"] == "sent", "the new owner completed the delivery"
    row = row_of(ctx, delivery_id)
    assert row["status"] == "sent"
    assert row["chunks_sent"] == row["chunk_count"]
    assert row["lease_owner"] is None
    # Only B's chunks were ever sent: A stopped before sending anything.
    assert len(fake_telegram.texts_to(MANAGER_ID)) == row["chunk_count"]


def test_takeover_mid_send_never_duplicates_a_chunk(ctx, fake_telegram):
    """A sends chunk 1, B takes over, A's progress write is refused.

    The chunk A already sent cannot be recorded, so B resolves the delivery
    conservatively (unknown outcome, one notice) instead of resending anything.
    """

    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=2, chunks_sent=0, snapshot=True)

    sender = ReportService(with_worker(ctx, "worker-a"))
    real_send = fake_telegram.send_message
    state = {"took_over": False, "b_outcome": None}

    def send_and_take_over(chat_id, text, **kwargs):
        message_id = real_send(chat_id, text, **kwargs)
        if not state["took_over"]:
            state["took_over"] = True
            # A's lease expires while the first chunk is in flight, and B runs
            # its own delivery attempt for the same row before A gets to write.
            expire_lease(ctx)
            state["b_outcome"] = ReportService(
                with_worker(ctx, "worker-b")
            )._deliver(delivery_id, ctx.now())
        return message_id

    fake_telegram.send_message = send_and_take_over
    try:
        outcome = sender._deliver(delivery_id, ctx.now())
    finally:
        fake_telegram.send_message = real_send

    assert outcome == "lease_lost"
    # B settled the abandoned delivery while A was still holding the response of
    # chunk 1, so B never sent anything and nothing was duplicated.
    assert state["b_outcome"] == "uncertain"
    chunk_attempts = [item for item in fake_telegram.attempts if item["chat_id"] == MANAGER_ID
                      and item["text"] != messages.REPORT_DELIVERY_UNKNOWN]
    assert len(chunk_attempts) == 1
    row = row_of(ctx, delivery_id)
    assert row["status"] == "failed_uncertain"
    assert row["chunks_sent"] == 0, "the stale progress write must not be applied"
    assert row["lease_owner"] is None
    assert messages.REPORT_DELIVERY_UNKNOWN in fake_telegram.texts_to(MANAGER_ID)


def test_making_ownership_required_is_enforced_by_the_api(ctx):
    """The fenced transitions require an explicit ownership decision."""

    at(ctx, "2026-11-23T20:00:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=1, chunks_sent=0)
    with pytest.raises(TypeError):
        repository.mark_report_sent(ctx.db.connection(), delivery_id=delivery_id, now=ctx.now_iso())
    with pytest.raises(TypeError):
        repository.update_report_progress(
            ctx.db.connection(),
            delivery_id=delivery_id,
            chunk_count=1,
            chunks_sent=0,
            telegram_message_ids=[],
            snapshot_json=None,
            narrative_used=False,
            now=ctx.now_iso(),
        )
    with pytest.raises(TypeError):
        repository.release_report_delivery(
            ctx.db.connection(),
            delivery_id=delivery_id,
            status="pending",
            error_category=None,
            next_attempt_at=None,
            now=ctx.now_iso(),
        )


def test_lease_lost_exception_is_reported_by_run_due(ctx, monkeypatch):
    """A run that loses its lease reports it instead of corrupting the row."""

    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(ctx, chunks=1, chunks_sent=0)
    take_over(ctx, delivery_id, worker_id="worker-other")

    service = ReportService(with_worker(ctx, "worker-a"))
    assert service._deliver(delivery_id, ctx.now()) == "busy"
    assert row_of(ctx, delivery_id)["lease_owner"] == "worker-other"

    # And an explicit lease-lost raise from a transition is translated, not raised.
    def raise_lost(self, row, manager_id, moment):
        raise DeliveryLeaseLost()

    monkeypatch.setattr(ReportService, "_send_delivery", raise_lost)
    with ctx.db.transaction() as connection:
        assert repository.release_report_delivery(
            connection,
            delivery_id=delivery_id,
            status="pending",
            error_category=None,
            next_attempt_at=None,
            now=ctx.now_iso(),
            worker_id="worker-other",
        ) == 1
    summary = service.run_due(ctx.now())
    assert summary["deliveries"].get("lease_lost") == 1
    assert report_deliveries(ctx)[0]["status"] == "sending"
