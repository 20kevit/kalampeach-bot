"""Cron-invoked worker: drain the durable inbox, deliver clarifications, send reports.

Every run is short-lived and bounded:

* at most ``WORKER_BATCH_SIZE`` updates;
* at most ``WORKER_MAX_SECONDS`` of wall-clock work;
* claims use a database lease with an expiry, and status transitions happen in
  ``BEGIN IMMEDIATE`` transactions, so overlapping cron invocations (or a
  restarted run after a kill) cannot double-process an update or a report.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from .. import migrations, repository
from ..timeutil import to_utc_iso
from .context import AppContext
from .errors import PermanentProcessingError, RetryableProcessingError
from .processor import MessageProcessor

logger = logging.getLogger("kalampeach.worker")


def backoff_seconds(attempts: int) -> int:
    """Bounded exponential backoff for retried updates."""

    attempt = max(1, int(attempts))
    return min(60 * (2 ** (attempt - 1)), 1800)


class Worker:
    def __init__(
        self,
        ctx: AppContext,
        *,
        batch_size: Optional[int] = None,
        max_seconds: Optional[float] = None,
        lease_seconds: Optional[int] = None,
        max_attempts: Optional[int] = None,
    ) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.batch_size = int(batch_size or self.settings.worker_batch_size)
        self.max_seconds = float(max_seconds or self.settings.worker_max_seconds)
        self.lease_seconds = int(lease_seconds or self.settings.worker_lease_seconds)
        self.max_attempts = int(max_attempts or self.settings.worker_max_attempts)
        self.processor = MessageProcessor(ctx)
        self._schema_checked = False

    # ------------------------------------------------------------------
    def ensure_schema(self) -> int:
        """Apply forward-only migrations (idempotent, safe under overlap)."""

        with self.ctx.db.connection() as connection:
            version = migrations.ensure_schema(connection)
        self._schema_checked = True
        return int(version)

    # ------------------------------------------------------------------
    def run_once(
        self, now: Optional[datetime] = None, *, skip_reports: bool = False
    ) -> Dict[str, object]:
        started = time.monotonic()
        moment = now or self.ctx.now()
        summary: Dict[str, object] = {
            "claimed": 0,
            "done": 0,
            "retried": 0,
            "failed": 0,
            "duplicates": 0,
            "clarifications": {},
            "reports": {},
            "elapsed_seconds": 0.0,
        }

        if not self._schema_checked:
            summary["schema_version"] = self.ensure_schema()

        with self.ctx.db.transaction() as connection:
            repository.expire_exhausted_updates(
                connection, now=to_utc_iso(moment), max_attempts=self.max_attempts
            )

        while int(summary["claimed"]) < self.batch_size:
            if time.monotonic() - started > self.max_seconds:
                break
            row = self._claim(moment)
            if row is None:
                break
            summary["claimed"] = int(summary["claimed"]) + 1
            self._process_row(row, moment, summary)

        summary["clarifications"] = self.processor.capture.deliver_pending_clarifications(moment)
        if not skip_reports:
            summary["reports"] = self.processor.reports.run_due(moment)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)

        logger.info(
            "worker_run claimed=%s done=%s retried=%s failed=%s duplicates=%s elapsed=%.3f",
            summary["claimed"],
            summary["done"],
            summary["retried"],
            summary["failed"],
            summary["duplicates"],
            summary["elapsed_seconds"],
        )
        return summary

    # ------------------------------------------------------------------
    def _claim(self, moment: datetime):
        with self.ctx.db.transaction() as connection:
            return repository.claim_next_update(
                connection,
                now=to_utc_iso(moment),
                lease_seconds=self.lease_seconds,
                max_attempts=self.max_attempts,
                worker_id=self.ctx.worker_id,
            )

    def _process_row(self, row, moment: datetime, summary: Dict[str, object]) -> None:
        update_id = int(row["update_id"])
        attempts = int(row["attempts"])
        try:
            outcome = self.processor.process(row)
        except PermanentProcessingError as exc:
            self._finish_failed(update_id, exc.category, exc.source_message_id)
            summary["failed"] = int(summary["failed"]) + 1
            logger.error("update_failed update_id=%s category=%s", update_id, exc.category)
            return
        except RetryableProcessingError as exc:
            self._retry(update_id, attempts, exc.category, moment, summary)
            return
        except Exception as exc:  # unexpected bug or transient infrastructure error
            logger.error(
                "update_error update_id=%s error=%s", update_id, exc.__class__.__name__
            )
            self._retry(update_id, attempts, "unexpected_error", moment, summary)
            return

        with self.ctx.db.transaction() as connection:
            repository.finish_update(
                connection, update_id=update_id, status="done", now=to_utc_iso(moment)
            )
        if outcome.ignored:
            summary["duplicates"] = int(summary["duplicates"]) + 1
        summary["done"] = int(summary["done"]) + 1

    def _retry(
        self, update_id: int, attempts: int, category: str, moment: datetime, summary
    ) -> None:
        if attempts >= self.max_attempts:
            self._finish_failed(update_id, category, None)
            summary["failed"] = int(summary["failed"]) + 1
            logger.error("update_failed update_id=%s category=%s attempts=%d", update_id, category, attempts)
            return
        available_at = moment + timedelta(seconds=backoff_seconds(attempts))
        with self.ctx.db.transaction() as connection:
            repository.schedule_update_retry(
                connection,
                update_id=update_id,
                available_at=to_utc_iso(available_at),
                now=to_utc_iso(moment),
                error_category=category,
                error_detail="retry scheduled",
            )
        summary["retried"] = int(summary["retried"]) + 1
        logger.warning(
            "update_retry_scheduled update_id=%s category=%s attempt=%d", update_id, category, attempts
        )

    def _finish_failed(
        self, update_id: int, category: str, source_message_id: Optional[int]
    ) -> None:
        now_iso = to_utc_iso(self.ctx.now())
        with self.ctx.db.transaction() as connection:
            repository.finish_update(
                connection,
                update_id=update_id,
                status="failed",
                now=now_iso,
                error_category=category,
                error_detail="permanent failure",
            )
            if source_message_id is not None:
                repository.set_source_status(
                    connection,
                    source_message_id=int(source_message_id),
                    processing_status="failed",
                    failure_reason=category,
                    now=now_iso,
                )
