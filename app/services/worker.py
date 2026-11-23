"""Short-lived cron worker.

Invoked by cPanel cron (ideally every minute). It:

1. fails abandoned claims that already used every attempt;
2. claims a bounded batch of inbox rows with a database lease;
3. processes them, settling each row durably;
4. runs any due daily report;
5. exits cleanly.

There is no daemon, thread, timer or in-process scheduler. Every step is safe
to repeat: overlapping invocations cannot claim the same row, and re-processing
an update cannot create a second event or revision.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.context import AppContext
from app.errors import (
    AIInvalidOutput,
    AIUnavailable,
    AppError,
    TelegramUnavailable,
)
from app.repo import inbox as inbox_repo
from app.services import processor
from app.services import outcome as outcomes
from app.services.outcome import Outcome
from app.services.reporting import run_due_reports
from app.timeutil import to_utc_iso

MAX_RUNTIME_CEILING_SECONDS = 300
CLAIM_CHUNK = 5


@dataclass
class WorkerStats:
    claimed: int = 0
    done: int = 0
    ignored: int = 0
    retried: int = 0
    failed: int = 0
    reports: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0
    stopped_early: bool = False

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "done": self.done,
            "ignored": self.ignored,
            "retried": self.retried,
            "failed": self.failed,
            "reports": self.reports,
            "duration_seconds": round(self.duration_seconds, 3),
            "stopped_early": self.stopped_early,
        }


def _backoff_seconds(attempts: int) -> int:
    return min(30 * (2 ** max(attempts - 1, 0)), 900)


def _settle(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    token: str,
    outcome: Outcome,
    now: datetime,
    stats: WorkerStats,
) -> None:
    now_iso = to_utc_iso(now)
    attempts = int(row["attempts"])
    inbox_id = int(row["id"])

    if outcome.kind == outcomes.DONE:
        inbox_repo.mark_done(
            conn, inbox_id=inbox_id, token=token, now_iso=now_iso,
            error_category=outcome.detail or None,
        )
        stats.done += 1
        return
    if outcome.kind == outcomes.IGNORED:
        inbox_repo.mark_ignored(
            conn, inbox_id=inbox_id, token=token, now_iso=now_iso, reason=outcome.detail or "ignored"
        )
        stats.ignored += 1
        return
    if outcome.kind == outcomes.RETRY:
        if attempts >= ctx.config.inbox_max_attempts:
            inbox_repo.mark_failed(
                conn, inbox_id=inbox_id, token=token, now_iso=now_iso,
                error_category=outcome.category or "RETRY_EXHAUSTED",
            )
            stats.failed += 1
            return
        delay = outcome.retry_after_seconds or _backoff_seconds(attempts)
        inbox_repo.mark_retry(
            conn,
            inbox_id=inbox_id,
            token=token,
            now_iso=now_iso,
            next_attempt_iso=to_utc_iso(now + timedelta(seconds=delay)),
            error_category=outcome.category or "RETRY",
        )
        stats.retried += 1
        return

    inbox_repo.mark_failed(
        conn, inbox_id=inbox_id, token=token, now_iso=now_iso,
        error_category=outcome.category or "FAILED",
    )
    stats.failed += 1


def _outcome_for_exception(exc: BaseException) -> Outcome:
    if isinstance(exc, (AIUnavailable, TelegramUnavailable)):
        return outcomes.retry(exc.category)
    if isinstance(exc, AIInvalidOutput):
        return outcomes.failed(exc.category)
    if isinstance(exc, AppError):
        return outcomes.retry(exc.category) if exc.retryable else outcomes.failed(exc.category)
    return outcomes.failed("INTERNAL_ERROR")


def run_worker(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    max_runtime_seconds: int | None = None,
    batch_size: int | None = None,
    collect_reports: bool = True,
) -> WorkerStats:
    started = time.monotonic()
    runtime_budget = min(
        max_runtime_seconds or ctx.config.worker_max_runtime_seconds,
        MAX_RUNTIME_CEILING_SECONDS,
    )
    batch = batch_size or ctx.config.worker_batch_size
    stats = WorkerStats()
    token = uuid.uuid4().hex
    current_time = now or ctx.now()

    inbox_repo.fail_exhausted_claims(
        conn, now_iso=to_utc_iso(current_time), max_attempts=ctx.config.inbox_max_attempts
    )

    processed = 0
    while processed < batch:
        elapsed = time.monotonic() - started
        if elapsed >= runtime_budget:
            stats.stopped_early = True
            break
        chunk = min(CLAIM_CHUNK, batch - processed)
        rows = inbox_repo.claim_batch(
            conn,
            token=token,
            now_iso=to_utc_iso(current_time),
            lease_until_iso=to_utc_iso(
                current_time + timedelta(seconds=runtime_budget + 60)
            ),
            limit=chunk,
            max_attempts=ctx.config.inbox_max_attempts,
        )
        if not rows:
            break
        for row in rows:
            if time.monotonic() - started >= runtime_budget:
                stats.stopped_early = True
                break
            stats.claimed += 1
            processed += 1
            step_now = current_time
            try:
                outcome = processor.process_inbox_row(ctx, conn, row, now=step_now)
            except BaseException as exc:  # noqa: BLE001 - categorised, never re-raised
                outcome = _outcome_for_exception(exc)
                ctx.logger.error(
                    "update_processing_failed",
                    extra={
                        "fields": {
                            "inbox_id": int(row["id"]),
                            "category": outcome.category or "INTERNAL_ERROR",
                            "exception": type(exc).__name__,
                        }
                    },
                )
            try:
                _settle(
                    ctx,
                    conn,
                    row=row,
                    token=token,
                    outcome=outcome,
                    now=step_now,
                    stats=stats,
                )
            except sqlite3.Error:
                # A settled row is recoverable by lease expiry; do not abort the batch.
                ctx.logger.error(
                    "update_settle_failed",
                    extra={"fields": {"inbox_id": int(row["id"])}},
                )
        if stats.stopped_early:
            break

    # Anything claimed but not processed (runtime limit) goes straight back to
    # the queue instead of waiting for its lease to expire.
    try:
        inbox_repo.release_claims(conn, token=token, now_iso=to_utc_iso(current_time))
    except sqlite3.Error:
        ctx.logger.error("claim_release_failed", extra={"fields": {"token_scope": "worker"}})

    if collect_reports and time.monotonic() - started < runtime_budget:
        try:
            stats.reports = run_due_reports(ctx, conn, now=current_time)
        except Exception as exc:  # noqa: BLE001 - report failures must not stop the worker
            ctx.logger.error(
                "report_run_failed", extra={"fields": {"exception": type(exc).__name__}}
            )

    stats.duration_seconds = time.monotonic() - started
    return stats
