"""TEST-PLAN B: cron worker claiming, recovery, retries, overlap and bounds."""

from __future__ import annotations

import threading
import time as time_module

import pytest

from kalampeach import migrations, repository
from kalampeach.services.worker import Worker, backoff_seconds
from kalampeach.timeutil import to_utc_iso
from conftest import extraction
from helpers import (
    enqueue,
    events,
    fetch_all,
    fetch_one,
    run_worker,
    telegram_update,
)


def test_claim_leases_and_counts_attempts(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    now = to_utc_iso(ctx.now())
    with ctx.db.transaction() as connection:
        row = repository.claim_next_update(
            connection,
            now=now,
            lease_seconds=60,
            max_attempts=3,
            worker_id="worker-a",
        )
    assert row is not None
    assert row["status"] == "processing"
    assert row["lease_owner"] == "worker-a"
    assert row["attempts"] == 1

    # A second claim finds nothing: the row is leased.
    with ctx.db.transaction() as connection:
        assert (
            repository.claim_next_update(
                connection, now=now, lease_seconds=60, max_attempts=3, worker_id="worker-b"
            )
            is None
        )


def test_expired_lease_is_recovered(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    started = ctx.now()
    with ctx.db.transaction() as connection:
        repository.claim_next_update(
            connection,
            now=to_utc_iso(started),
            lease_seconds=30,
            max_attempts=5,
            worker_id="crashed-worker",
        )
    # The worker died without finishing; time passes.
    ctx.clock.advance(minutes=5)
    fake_request = extraction(amount="10", currency="تومان")
    ctx.ai.extractions.append(fake_request)

    summary = run_worker(ctx, skip_reports=True)
    assert summary["done"] == 1
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert len(events(ctx)) == 1


def test_two_connections_cannot_claim_one_update(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    now = to_utc_iso(ctx.now())
    with ctx.db.transaction() as first:
        row = repository.claim_next_update(
            first, now=now, lease_seconds=60, max_attempts=3, worker_id="a"
        )
        assert row is not None
    # The first claim is committed and still leased, so a second worker gets
    # nothing even though a queue row exists.
    with ctx.db.transaction() as second:
        assert (
            repository.claim_next_update(
                second, now=now, lease_seconds=60, max_attempts=3, worker_id="b"
            )
            is None
        )


def test_exhausted_leases_are_marked_failed(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    now_iso = to_utc_iso(ctx.now())
    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE telegram_updates SET status='processing', attempts=3, "
            "lease_owner='dead', lease_expires_at_utc=? WHERE update_id=1",
            (now_iso,),
        )
    with ctx.db.transaction() as connection:
        changed = repository.expire_exhausted_updates(
            connection, now=now_iso, max_attempts=3
        )
    assert changed == 1
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "failed"
    assert row["last_error_category"] == "lease_expired"


def test_batch_size_is_respected(ctx, fake_ai):
    for index in range(5):
        enqueue(ctx, telegram_update(update_id=index + 1, message_id=10 + index, text="فروش"))
        fake_ai.extractions.append(extraction(amount="10", currency="تومان"))

    worker = Worker(ctx, batch_size=2)
    summary = worker.run_once(skip_reports=True)

    assert summary["claimed"] == 2
    assert len(events(ctx)) == 2
    queued = fetch_all(ctx, "SELECT * FROM telegram_updates WHERE status='queued'")
    assert len(queued) == 3


def test_runtime_budget_stops_the_run(ctx, monkeypatch):
    for index in range(3):
        enqueue(ctx, telegram_update(update_id=index + 1, message_id=10 + index, text="فروش"))

    def slow_extract(request):
        time_module.sleep(0.02)
        return extraction(amount="10", currency="تومان")

    monkeypatch.setattr(ctx.ai, "extract_event", slow_extract)
    worker = Worker(ctx, batch_size=10, max_seconds=0.005)
    summary = worker.run_once(skip_reports=True)

    assert summary["claimed"] == 1
    assert summary["elapsed_seconds"] >= 0


def test_retry_backoff_is_bounded(ctx, fake_ai):
    from kalampeach.ai.types import AIUnavailable

    assert backoff_seconds(1) == 60
    assert backoff_seconds(2) == 120
    assert backoff_seconds(3) == 240
    assert backoff_seconds(99) == 1800

    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    fake_ai.extractions.append(AIUnavailable("down"))
    run_worker(ctx, skip_reports=True)

    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["available_at_utc"] == "2026-11-23T12:01:00Z"


def test_retries_stop_after_max_attempts(ctx, fake_ai):
    from kalampeach.ai.types import AIUnavailable

    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    for _ in range(4):
        fake_ai.extractions.append(AIUnavailable("down"))

    worker = Worker(ctx, max_attempts=2)
    worker.run_once(skip_reports=True)
    ctx.clock.advance(hours=1)
    worker.run_once(skip_reports=True)

    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "failed"
    assert row["last_error_category"] == "ai_unavailable"
    assert events(ctx) == []


def test_unexpected_errors_are_retried_then_observable(ctx, monkeypatch):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))

    def boom(self, payload):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("kalampeach.services.processor.MessageProcessor.process", boom)
    worker = Worker(ctx, max_attempts=2)
    summary = worker.run_once(skip_reports=True)
    assert summary["retried"] == 1

    ctx.clock.advance(hours=1)
    summary = worker.run_once(skip_reports=True)
    assert summary["failed"] == 1
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "failed"
    assert row["last_error_category"] == "unexpected_error"


def test_worker_applies_migrations_on_a_fresh_database(
    settings, fake_ai, fake_telegram, clock
):
    from kalampeach.db import Database
    from kalampeach.services.context import AppContext

    settings.database_path.unlink(missing_ok=True)  # simulate a brand-new deployment
    db = Database(settings.database_path, journal_mode="WAL")
    ctx = AppContext(
        settings=settings,
        db=db,
        ai=fake_ai,
        telegram=fake_telegram,
        clock=clock,
        worker_id="fresh-worker",
    )
    summary = Worker(ctx).run_once(skip_reports=True)
    expected = migrations.latest_version()
    assert summary["schema_version"] == expected
    with db.connection() as connection:
        assert migrations.current_version(connection) == expected


def test_overlapping_worker_runs_process_each_update_once(ctx, fake_ai):
    total = 8
    for index in range(total):
        enqueue(
            ctx,
            telegram_update(update_id=index + 1, message_id=100 + index, text="فروش"),
        )
        fake_ai.extractions.append(extraction(amount="10", currency="تومان"))

    results = []
    errors = []

    def work():
        try:
            worker = Worker(ctx, batch_size=total)
            results.append(worker.run_once(skip_reports=True))
        except Exception as exc:  # pragma: no cover - surfaces in assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(events(ctx)) == total
    assert fake_ai.calls.count("extract_event") == total
    done = fetch_one(
        ctx, "SELECT COUNT(*) AS total FROM telegram_updates WHERE status='done'"
    )
    assert done["total"] == total
    assert sum(int(result["claimed"]) for result in results) == total


def test_worker_is_idempotent_when_run_with_no_work(ctx):
    summary = run_worker(ctx, skip_reports=True)
    assert summary["claimed"] == 0
    assert summary["done"] == 0
    assert summary["failed"] == 0


def test_worker_skips_reports_when_asked(ctx, fake_ai):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    fake_ai.extractions.append(extraction(amount="10", currency="تومان"))
    summary = run_worker(ctx, skip_reports=True)
    assert summary["reports"] == {}


# ---------------------------------------------------------------------------
# Lease ownership: a worker that lost its lease must not overwrite the run that
# took over. Verified defect: finish/retry updates were keyed by update_id only.
# ---------------------------------------------------------------------------
def _claim_with(ctx, worker_id: str):
    with ctx.db.transaction() as connection:
        return repository.claim_next_update(
            connection,
            now=to_utc_iso(ctx.now()),
            lease_seconds=30,
            max_attempts=5,
            worker_id=worker_id,
        )


def test_lost_lease_cannot_finish_another_runs_update(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))

    assert _claim_with(ctx, "worker-a") is not None
    ctx.clock.advance(minutes=5)  # worker A froze past its lease
    assert _claim_with(ctx, "worker-b") is not None

    with ctx.db.transaction() as connection:
        changed = repository.finish_update(
            connection,
            update_id=1,
            status="done",
            now=to_utc_iso(ctx.now()),
            worker_id="worker-a",
        )

    assert changed == 0
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "processing"
    assert row["lease_owner"] == "worker-b"

    with ctx.db.transaction() as connection:
        assert (
            repository.finish_update(
                connection,
                update_id=1,
                status="done",
                now=to_utc_iso(ctx.now()),
                worker_id="worker-b",
            )
            == 1
        )
    assert fetch_one(ctx, "SELECT status FROM telegram_updates WHERE update_id = 1")["status"] == "done"


def test_lost_lease_cannot_requeue_another_runs_update(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    _claim_with(ctx, "worker-a")
    ctx.clock.advance(minutes=5)
    _claim_with(ctx, "worker-b")

    with ctx.db.transaction() as connection:
        changed = repository.schedule_update_retry(
            connection,
            update_id=1,
            available_at=to_utc_iso(ctx.now()),
            now=to_utc_iso(ctx.now()),
            error_category="ai_unavailable",
            worker_id="worker-a",
        )

    assert changed == 0
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "processing"
    assert row["lease_owner"] == "worker-b"
    assert row["last_error_category"] != "ai_unavailable"


def test_zombie_worker_reports_a_lost_lease_instead_of_overwriting(ctx, fake_ai, fake_telegram):
    from helpers import with_worker

    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    worker_a = Worker(with_worker(ctx, "worker-a"))
    row_a = worker_a._claim(ctx.now())
    assert row_a is not None

    ctx.clock.advance(minutes=5)
    worker_b = Worker(with_worker(ctx, "worker-b"))
    assert worker_b._claim(ctx.now()) is not None

    # Worker A wakes up and finishes its (stale) row.
    summary = {
        "claimed": 1,
        "done": 0,
        "retried": 0,
        "failed": 0,
        "duplicates": 0,
        "lease_lost": 0,
    }
    fake_ai.extractions.append(extraction(amount="10", currency="تومان"))
    worker_a._process_row(row_a, ctx.now(), summary)

    assert summary["lease_lost"] == 1
    assert summary["done"] == 0
    row = fetch_one(ctx, "SELECT * FROM telegram_updates WHERE update_id = 1")
    assert row["status"] == "processing"
    assert row["lease_owner"] == "worker-b"


def test_uncontested_slow_worker_may_still_finish(ctx):
    enqueue(ctx, telegram_update(update_id=1, message_id=10, text="فروش"))
    _claim_with(ctx, "worker-a")
    ctx.clock.advance(minutes=5)  # lease expired, but nobody took over

    with ctx.db.transaction() as connection:
        changed = repository.finish_update(
            connection,
            update_id=1,
            status="done",
            now=to_utc_iso(ctx.now()),
            worker_id="worker-a",
        )
    assert changed == 1, "ownership, not lease freshness, decides"


def test_a_failing_phase_does_not_starve_the_others(ctx, monkeypatch):
    seed_event = __import__("helpers").seed_event
    seed_event(ctx, 1, amount="1000")
    ctx.clock.set(__import__("datetime").datetime(2026, 11, 23, 20, 0, 5, tzinfo=__import__("datetime").timezone.utc))

    def boom(*args, **kwargs):
        raise RuntimeError("clarification delivery is broken")

    monkeypatch.setattr(
        "kalampeach.services.capture.CaptureService.deliver_pending_clarifications", boom
    )
    summary = run_worker(ctx)

    assert summary["phase_failures"] == {"clarifications": "RuntimeError"}
    # The daily report still ran and was delivered.
    assert summary["reports"]["deliveries"] == {"sent": 1}
