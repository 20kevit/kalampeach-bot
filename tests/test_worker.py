"""Durable inbox, overlap, recovery, retries and bounded batches (TEST-PLAN B/G)."""

from __future__ import annotations

from datetime import timedelta

from app.errors import AIUnavailable
from app.repo import inbox
from app.services.worker import run_worker
from app.timeutil import to_utc_iso
from tests.helpers import (
    FIXED_NOW,
    enqueue,
    group_update,
    non_business,
    sale_event,
)


def _inbox_rows(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM telegram_inbox ORDER BY id")]


def _event_count(ctx):
    with ctx.db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _run(ctx, now=FIXED_NOW, **kwargs):
    with ctx.db.session() as conn:
        return run_worker(ctx, conn, now=now, collect_reports=False, **kwargs)


def test_worker_processes_queued_updates_and_settles_them(ctx, fake_ai):
    enqueue(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    fake_ai.queue_extraction(sale_event(amount="100"))
    stats = _run(ctx)
    assert stats.claimed == 1 and stats.done == 1
    rows = _inbox_rows(ctx)
    assert rows[0]["status"] == "done"
    assert rows[0]["processed_at_utc"] is not None
    assert rows[0]["attempts"] == 1
    assert _event_count(ctx) == 1


def test_empty_queue_is_a_clean_noop(ctx):
    stats = _run(ctx)
    assert stats.claimed == 0
    assert _inbox_rows(ctx) == []


def test_expired_claim_is_recovered_after_an_interrupted_run(ctx, fake_ai):
    enqueue(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    with ctx.db.session() as conn:
        conn.execute(
            "UPDATE telegram_inbox SET status = 'processing', claim_token = 'dead-worker', "
            "claimed_until_utc = ?, attempts = 1 WHERE update_id = 1",
            (to_utc_iso(FIXED_NOW - timedelta(minutes=5)),),
        )
    fake_ai.queue_extraction(sale_event(amount="100"))
    stats = _run(ctx)
    assert stats.done == 1
    assert _inbox_rows(ctx)[0]["status"] == "done"
    assert _event_count(ctx) == 1


def test_overlapping_worker_cannot_claim_a_live_lease(ctx, fake_ai):
    enqueue(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    with ctx.db.session() as conn:
        claimed = inbox.claim_batch(
            conn,
            token="other-worker",
            now_iso=to_utc_iso(FIXED_NOW),
            lease_until_iso=to_utc_iso(FIXED_NOW + timedelta(minutes=5)),
            limit=10,
            max_attempts=5,
        )
    assert len(claimed) == 1

    fake_ai.queue_extraction(sale_event(amount="100"))
    stats = _run(ctx)
    assert stats.claimed == 0
    assert _event_count(ctx) == 0

    # Once the lease expires the row is processed exactly once.
    later = FIXED_NOW + timedelta(minutes=10)
    stats = _run(ctx, now=later)
    assert stats.claimed == 1
    assert _event_count(ctx) == 1
    assert fake_ai.extraction_requests


def test_abandoned_claim_with_exhausted_attempts_becomes_observable(ctx):
    enqueue(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    with ctx.db.session() as conn:
        conn.execute(
            "UPDATE telegram_inbox SET status = 'processing', claim_token = 'x', "
            "attempts = 5, claimed_until_utc = ? WHERE update_id = 1",
            (to_utc_iso(FIXED_NOW - timedelta(minutes=15)),),
        )
    stats = _run(ctx)
    row = _inbox_rows(ctx)[0]
    assert row["status"] == "failed"
    assert row["last_error_category"] == "CLAIM_EXPIRED"
    assert stats.claimed == 0


def test_transient_failures_use_bounded_backoff_and_become_failed(ctx, fake_ai):
    enqueue(ctx, group_update(1, "فروش ۱۰۰ تومان"))
    fake_ai.extraction_error = AIUnavailable("timeout")
    now = FIXED_NOW
    seen_delays: list[int] = []
    for _attempt in range(1, 6):
        _run(ctx, now=now)
        row = _inbox_rows(ctx)[0]
        if row["status"] == "retry":
            seen_delays.append(int(row["attempts"]))
            now = now + timedelta(minutes=20)
        else:
            break
    row = _inbox_rows(ctx)[0]
    assert row["status"] == "failed"
    assert row["last_error_category"] == "AI_UNAVAILABLE"
    assert row["attempts"] == 5
    assert seen_delays == [1, 2, 3, 4]
    assert _event_count(ctx) == 0


def test_worker_respects_the_batch_size(ctx, fake_ai):
    for index in range(1, 6):
        enqueue(ctx, group_update(index, "سلام", message_id=1000 + index))
        fake_ai.queue_extraction(non_business())
    stats = _run(ctx, batch_size=2)
    assert stats.claimed == 2
    statuses = [row["status"] for row in _inbox_rows(ctx)]
    assert statuses.count("ignored") == 2
    assert statuses.count("queued") == 3


def test_release_claims_returns_unprocessed_work_immediately(ctx):
    enqueue(ctx, group_update(1, "سلام"))
    with ctx.db.session() as conn:
        claimed = inbox.claim_batch(
            conn,
            token="stopping-worker",
            now_iso=to_utc_iso(FIXED_NOW),
            lease_until_iso=to_utc_iso(FIXED_NOW + timedelta(minutes=5)),
            limit=10,
            max_attempts=5,
        )
        assert len(claimed) == 1
        released = inbox.release_claims(conn, token="stopping-worker", now_iso=to_utc_iso(FIXED_NOW))
    assert released == 1
    row = _inbox_rows(ctx)[0]
    assert row["status"] == "retry"
    assert row["claim_token"] is None


def test_settle_requires_the_claim_token(ctx):
    enqueue(ctx, group_update(1, "سلام"))
    with ctx.db.session() as conn:
        inbox.claim_batch(
            conn,
            token="worker-a",
            now_iso=to_utc_iso(FIXED_NOW),
            lease_until_iso=to_utc_iso(FIXED_NOW + timedelta(minutes=5)),
            limit=10,
            max_attempts=5,
        )
        inbox.mark_done(conn, inbox_id=1, token="worker-b", now_iso=to_utc_iso(FIXED_NOW))
    row = _inbox_rows(ctx)[0]
    assert row["status"] == "processing"


def test_malformed_payload_fails_without_retry_loop(ctx):
    with ctx.db.session() as conn:
        inbox.enqueue(
            conn,
            update_id=99,
            payload_json="{not-json",
            kind="message",
            now_iso=to_utc_iso(FIXED_NOW),
        )
    stats = _run(ctx)
    assert stats.failed == 1
    row = _inbox_rows(ctx)[0]
    assert row["status"] == "failed"
    assert row["last_error_category"] == "UPDATE_PAYLOAD_INVALID"
