"""Report scheduling and delivery.

Daily report
------------
* target: the configured manager, at ``DAILY_REPORT_TIME`` in ``Asia/Tehran``;
* the cron worker decides whether the report is due, using ``zoneinfo`` — never
  the host's cron timezone;
* a missed tick is recovered once inside the configured recovery window;
* ``report_deliveries.report_key`` (type + manager + local date) is UNIQUE, and
  row-level leases plus per-chunk progress make overlapping cron runs safe.

On-demand reports
-----------------
Only the manager may request one, only in private chat. The request row is keyed
by its source Telegram message, so a retried update cannot create two reports.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Mapping, Optional, Tuple

from .. import repository
from ..ai.types import AIError, NarrativeRequest
from ..telegram.client import TelegramError, chunk_text
from ..timeutil import (
    local_date,
    local_time_to_utc,
    parse_business_date,
    parse_utc_iso,
    to_utc_iso,
)
from . import messages, reporting
from .context import AppContext
from .notify import Notifier
from .outcomes import ServiceOutcome

logger = logging.getLogger("kalampeach.reports")

REPORT_MAX_ATTEMPTS = 3
LATE_AFTER = timedelta(minutes=10)
MAX_GAP_SCAN_DAYS = 30


class ReportService:
    def __init__(self, ctx: AppContext, notifier: Optional[Notifier] = None) -> None:
        self.ctx = ctx
        self.notifier = notifier or Notifier(ctx)

    # ------------------------------------------------------------------
    # Daily schedule
    # ------------------------------------------------------------------
    def scheduled_instant(self, local_day: date) -> datetime:
        return local_time_to_utc(
            local_day, self.ctx.settings.daily_report_time, self.ctx.timezone
        )

    def run_due(self, now: Optional[datetime] = None, *, limit: int = 5) -> Dict[str, object]:
        moment = now or self.ctx.now()
        now_iso = to_utc_iso(moment)
        timezone = self.ctx.timezone
        settings = self.ctx.settings

        today = local_date(moment, timezone)
        schedule = settings.daily_report_time
        scheduled_today = local_time_to_utc(today, schedule, timezone)

        candidates: List[Tuple[date, datetime]] = []
        if moment >= scheduled_today:
            candidates.append((today, scheduled_today))

        missed, recoverable = self._scan_missed_days(moment, now_iso)
        if recoverable is not None:
            candidates.append(recoverable)
        if missed:
            logger.warning("daily_report_gap_recorded days=%d", missed)

        created = 0
        for local_day, scheduled in candidates:
            created += self._ensure_daily_row(local_day, scheduled, now_iso)

        deliveries: Dict[str, int] = {}
        with self.ctx.db.connection() as connection:
            due_rows = repository.list_report_deliveries_due(
                connection, now=now_iso, limit=limit
            )
        for row in due_rows:
            delivery_id = int(row["id"])
            try:
                outcome = self._deliver(delivery_id, moment)
            except Exception as exc:
                # One broken delivery must not starve the rest of the queue.
                logger.error(
                    "report_delivery_error delivery_id=%s error=%s",
                    delivery_id,
                    exc.__class__.__name__,
                )
                outcome = "errored"
            deliveries[outcome] = deliveries.get(outcome, 0) + 1

        return {"daily_created": created, "deliveries": deliveries}

    def _scan_missed_days(
        self, moment: datetime, now_iso: str
    ) -> Tuple[int, Optional[Tuple[date, datetime]]]:
        """Find missed 23:30 ticks behind ``today``.

        The most recent day inside the recovery window is returned as a
        recoverable candidate (it is created and sent normally). Older days are
        recorded once as ``skipped`` so a multi-day outage is visible to
        operators, but are never sent late — a stale daily report has little
        value and would be confused with the current one.
        """

        timezone = self.ctx.timezone
        schedule = self.ctx.settings.daily_report_time
        recovery = timedelta(hours=self.ctx.settings.daily_report_recovery_hours)
        manager = self.ctx.settings.manager_telegram_id

        recorded = 0
        recoverable: Optional[Tuple[date, datetime]] = None
        day = local_date(moment, timezone) - timedelta(days=1)

        for _ in range(MAX_GAP_SCAN_DAYS):
            scheduled = local_time_to_utc(day, schedule, timezone)
            if moment < scheduled:
                break
            report_key = "daily:%d:%s" % (manager, day.isoformat())
            with self.ctx.db.connection() as connection:
                if repository.get_report_delivery_by_key(connection, report_key) is not None:
                    break
            if not self._bot_was_active_before(to_utc_iso(scheduled)):
                # Nothing was recorded before that tick; there is no gap to report.
                break
            if moment - scheduled <= recovery:
                if recoverable is None:
                    recoverable = (day, scheduled)
            else:
                if self._ensure_daily_row(day, scheduled, now_iso, status="skipped"):
                    recorded += 1
            day -= timedelta(days=1)

        return recorded, recoverable

    def _bot_was_active_before(self, moment_utc: str) -> bool:
        """True when the bot had already received messages before that instant."""

        with self.ctx.db.connection() as connection:
            return repository.count_source_messages_before(connection, moment_utc) > 0

    def _ensure_daily_row(
        self, local_day: date, scheduled: datetime, now_iso: str, *, status: str = "pending"
    ) -> int:
        manager = self.ctx.settings.manager_telegram_id
        report_key = "daily:%d:%s" % (manager, local_day.isoformat())
        with self.ctx.db.transaction() as connection:
            _row, created = repository.ensure_report_delivery(
                connection,
                report_key=report_key,
                report_type="daily",
                variant="daily",
                manager_telegram_user_id=manager,
                period_start_date=local_day.isoformat(),
                period_end_date=local_day.isoformat(),
                scheduled_at_utc=to_utc_iso(scheduled),
                now=now_iso,
                status=status,
            )
        if not created:
            return 0
        if status == "skipped":
            logger.warning("daily_report_missed local_date=%s", local_day.isoformat())
        return 1

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def _deliver(self, delivery_id: int, moment: datetime) -> str:
        now_iso = to_utc_iso(moment)
        bookkeeping_outcome = None
        manager_id = int(self.ctx.settings.manager_telegram_id)
        row = None

        with self.ctx.db.transaction() as connection:
            row = repository.get_report_delivery(connection, delivery_id)
            if row is None:
                return "missing"
            manager_id = int(row["manager_telegram_user_id"])
            status = str(row["status"])
            if status == "sent":
                return "already_sent"
            if status in ("failed_uncertain", "skipped"):
                return status

            if status == "sending":
                # A previous run claimed this delivery and never recorded how far
                # it got. Resolve that before touching Telegram again.
                outcome, bookkeeping_outcome = self._resolve_interrupted_delivery(
                    connection, row, now_iso
                )
                if outcome is not None:
                    resolved = outcome
                else:
                    row = repository.get_report_delivery(connection, delivery_id)
                    resolved = None
            else:
                resolved = None

            if resolved is None and row is not None:
                if int(row["attempts"]) >= REPORT_MAX_ATTEMPTS:
                    repository.release_report_delivery(
                        connection,
                        delivery_id=delivery_id,
                        status="failed",
                        error_category="retry_exhausted",
                        next_attempt_at=None,
                        now=now_iso,
                    )
                    logger.error("report_retry_exhausted delivery_id=%s", delivery_id)
                    resolved = "exhausted"
                else:
                    claimed = repository.claim_report_delivery(
                        connection,
                        delivery_id=delivery_id,
                        now=now_iso,
                        lease_seconds=self.ctx.settings.worker_lease_seconds,
                        worker_id=self.ctx.worker_id,
                    )
                    if not claimed:
                        resolved = "busy"
                    else:
                        row = repository.get_report_delivery(connection, delivery_id)

        # Telegram and notification work never happens inside a write transaction.
        if bookkeeping_outcome == "uncertain":
            self._notify_manager_uncertain(manager_id)
        if resolved is not None:
            return resolved
        if row is None:  # pragma: no cover - defensive
            return "missing"

        try:
            built = self._load_or_build(row, moment)
        except Exception as exc:  # computation or storage failure
            logger.error("report_build_failed delivery_id=%s error=%s", delivery_id, exc.__class__.__name__)
            self._release(delivery_id, "failed", "report_build_failed", None, moment)
            return "failed"

        if built is None:
            # The snapshot is gone but chunks were already confirmed: rebuilding
            # would resend confirmed content, so stop and ask for a human look.
            return self._mark_uncertain(
                manager_id,
                delivery_id,
                "snapshot_unavailable_with_confirmed_chunks",
                moment,
            )

        snapshot, chunks = built
        narrative_used = bool(snapshot.get("narrative_used"))
        message_ids = _message_ids(row)
        sent_upto = int(row["chunks_sent"])
        if sent_upto > len(chunks):
            return self._mark_uncertain(
                manager_id,
                delivery_id,
                "delivery_progress_inconsistent",
                moment,
            )

        for index in range(sent_upto, len(chunks)):
            try:
                message_id = self.ctx.telegram.send_message(manager_id, chunks[index])
            except TelegramError as exc:
                outcome = self._after_send_failure(delivery_id, exc, moment)
                if outcome == "uncertain":
                    self._notify_manager_uncertain(manager_id)
                return outcome
            message_ids.append(int(message_id))
            with self.ctx.db.transaction() as connection:
                repository.update_report_progress(
                    connection,
                    delivery_id=delivery_id,
                    chunk_count=len(chunks),
                    chunks_sent=index + 1,
                    telegram_message_ids=message_ids,
                    snapshot_json=None,
                    narrative_used=narrative_used,
                    now=to_utc_iso(moment),
                )

        with self.ctx.db.transaction() as connection:
            repository.mark_report_sent(
                connection, delivery_id=delivery_id, now=to_utc_iso(moment)
            )
        logger.info("report_sent delivery_id=%s chunks=%d", delivery_id, len(chunks))
        return "sent"

    def _resolve_interrupted_delivery(self, connection, row, now_iso: str):
        """Resolve a delivery left in ``sending`` by a dead worker.

        Returns ``(outcome, pending_notice)``: ``outcome`` is a final result when
        the row was settled, or ``None`` when it is safe to claim and continue
        (nothing was ever attempted). ``pending_notice`` is ``"uncertain"`` when
        the caller must tell the manager after the transaction commits.
        """

        delivery_id = int(row["id"])
        lease_expiry = row["lease_expires_at_utc"]
        if lease_expiry is not None and str(lease_expiry) > now_iso:
            return "busy", None  # another live worker still owns this delivery

        chunk_count = int(row["chunk_count"] or 0)
        chunks_sent = int(row["chunks_sent"] or 0)
        if chunk_count > 0 and chunks_sent >= chunk_count:
            # Every chunk was confirmed; only the final bookkeeping was lost.
            repository.mark_report_sent(connection, delivery_id=delivery_id, now=now_iso)
            logger.warning("report_delivery_recovered delivery_id=%s chunks=%d", delivery_id, chunk_count)
            return "sent_recovered", None
        if chunk_count == 0:
            # Nothing was built or sent: safe to retry from scratch.
            repository.release_report_delivery(
                connection,
                delivery_id=delivery_id,
                status="pending",
                error_category="interrupted_before_send",
                next_attempt_at=None,
                now=now_iso,
            )
            return None, None
        # At least one chunk is in flight with an unknown outcome.
        self._release_in_transaction(
            connection, delivery_id, "failed_uncertain", "chunk_outcome_unknown", now_iso
        )
        return "uncertain", "uncertain"

    def _mark_uncertain(
        self, manager_id: int, delivery_id: int, category: str, moment: datetime
    ) -> str:
        with self.ctx.db.transaction() as connection:
            self._release_in_transaction(
                connection, delivery_id, "failed_uncertain", category, to_utc_iso(moment)
            )
        logger.error(
            "report_delivery_uncertain delivery_id=%s category=%s", delivery_id, category
        )
        self._notify_manager_uncertain(manager_id)
        return "uncertain"

    def _release_in_transaction(
        self, connection, delivery_id: int, status: str, category: str, now_iso: str
    ) -> None:
        repository.release_report_delivery(
            connection,
            delivery_id=delivery_id,
            status=status,
            error_category=category,
            next_attempt_at=None,
            now=now_iso,
        )

    def _notify_manager_uncertain(self, manager_id: int) -> None:
        self.notifier.send(manager_id, messages.REPORT_DELIVERY_UNKNOWN, kind="notice")

    def _load_or_build(self, row, moment: datetime) -> Optional[Tuple[dict, List[str]]]:
        """Return ``(snapshot, chunks)`` to send, or ``None`` if it must not be rebuilt.

        A report is frozen in ``snapshot_json`` before its first send, so a retry
        always sends exactly the content that was computed for that period, even
        if events changed in the meantime. If the snapshot is unreadable while
        chunks are already confirmed, rebuilding would silently resend confirmed
        content; the caller treats that as an unknown outcome instead.
        """

        stored = None
        if row["snapshot_json"]:
            try:
                candidate = json.loads(row["snapshot_json"])
                if isinstance(candidate, dict) and isinstance(candidate.get("text"), str):
                    stored = candidate
            except json.JSONDecodeError:
                stored = None

        if stored is not None:
            return stored, chunk_text(stored["text"])

        if int(row["chunks_sent"] or 0) > 0:
            return None

        start = parse_business_date(row["period_start_date"])
        end = parse_business_date(row["period_end_date"])
        period = reporting.Period(start, end)
        is_daily = row["report_type"] == "daily"
        variant = "summary" if is_daily else (row["variant"] or "summary")

        with self.ctx.db.connection() as connection:
            facts = reporting.compute_facts(
                connection,
                period=period,
                timezone_name=self.ctx.settings.business_timezone_name,
                generated_at_utc=to_utc_iso(moment),
            )

        narrative = self._narrative(facts, variant, period)
        text = reporting.format_report(
            facts,
            report_type=variant,
            narrative=narrative,
            late=self._is_late(row, moment),
            title="گزارش روزانه" if is_daily else None,
        )
        snapshot = {"facts": facts, "text": text, "narrative_used": bool(narrative)}
        chunks = chunk_text(text)

        with self.ctx.db.transaction() as connection:
            repository.update_report_progress(
                connection,
                delivery_id=int(row["id"]),
                chunk_count=len(chunks),
                chunks_sent=0,
                telegram_message_ids=[],
                snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                narrative_used=bool(narrative),
                now=to_utc_iso(moment),
            )
        return snapshot, chunks

    def _narrative(self, facts: Mapping[str, object], variant: str, period) -> Optional[str]:
        try:
            result = self.ctx.ai.write_narrative(
                NarrativeRequest(
                    report_type=variant,
                    period_start=period.start.isoformat(),
                    period_end=period.end.isoformat(),
                    facts=dict(facts),
                )
            )
        except AIError as exc:
            logger.warning("report_narrative_unavailable category=%s", exc.category)
            return None
        if not reporting.narrative_is_safe(result.narrative, facts):
            logger.warning("report_narrative_rejected reason=unsupported_numbers_or_profit_language")
            return None
        return result.narrative

    def _is_late(self, row, moment: datetime) -> bool:
        if row["report_type"] != "daily":
            return False
        try:
            scheduled = parse_utc_iso(row["scheduled_at_utc"])
        except Exception:  # pragma: no cover - defensive
            return False
        return moment - scheduled > LATE_AFTER

    def _after_send_failure(self, delivery_id: int, exc: TelegramError, moment: datetime) -> str:
        if exc.uncertain:
            # Telegram might have delivered it; never resend automatically.
            self._release(delivery_id, "failed_uncertain", exc.category, None, moment)
            logger.error(
                "report_delivery_uncertain delivery_id=%s category=%s", delivery_id, exc.category
            )
            return "uncertain"
        if exc.retryable:
            with self.ctx.db.connection() as connection:
                row = repository.get_report_delivery(connection, delivery_id)
            attempts = int(row["attempts"]) if row else 1
            delay = min(60 * (2 ** max(0, attempts - 1)), 1800)
            self._release(
                delivery_id,
                "pending",
                exc.category,
                moment + timedelta(seconds=delay),
                moment,
            )
            logger.warning(
                "report_delivery_retry delivery_id=%s category=%s delay=%d",
                delivery_id,
                exc.category,
                delay,
            )
            return "retry"
        self._release(delivery_id, "failed", exc.category, None, moment)
        logger.error(
            "report_delivery_failed delivery_id=%s category=%s", delivery_id, exc.category
        )
        return "failed"

    def _release(
        self,
        delivery_id: int,
        status: str,
        category: Optional[str],
        next_attempt_at: Optional[datetime],
        moment: datetime,
    ) -> None:
        with self.ctx.db.transaction() as connection:
            repository.release_report_delivery(
                connection,
                delivery_id=delivery_id,
                status=status,
                error_category=category,
                next_attempt_at=to_utc_iso(next_attempt_at) if next_attempt_at else None,
                now=to_utc_iso(moment),
            )

    # ------------------------------------------------------------------
    # On-demand reports (manager only)
    # ------------------------------------------------------------------
    def handle_manager_request(
        self, *, message, source_message_id: int, parsed
    ) -> ServiceOutcome:
        settings = self.ctx.settings
        manager_id = int(settings.manager_telegram_id)
        if message.chat_type != "private":
            self.notifier.send(manager_id, messages.REPORT_ONLY_IN_PRIVATE, kind="notice")
            return ServiceOutcome(action="ignored", note="report_request_in_group")

        now_iso = self.ctx.now_iso()
        with self.ctx.db.transaction() as connection:
            request_row = repository.ensure_report_request(
                connection,
                source_message_id=source_message_id,
                manager_telegram_user_id=manager_id,
                request_text=message.text or "",
                now=now_iso,
            )
        request_id = int(request_row["id"])
        report_type = parsed.report_type or "summary"

        today = local_date(self.ctx.now(), self.ctx.timezone)
        try:
            period = self._period_from_parsed(parsed, today)
        except reporting.PeriodError as exc:
            reason = exc.reason
            if reason == "ambiguous_period":
                question = parsed.clarification_question or messages.REPORT_PERIOD_QUESTION
                self._ask_for_period(request_id, manager_id, question, report_type)
            else:
                self._ask_for_period(request_id, manager_id, messages.REPORT_RANGE_INVALID, report_type)
            return ServiceOutcome(action="awaiting_period", note=reason)

        return self._start_on_demand(
            request_id=request_id,
            manager_id=manager_id,
            period=period,
            report_type=report_type,
        )

    def handle_period_answer(
        self, *, message, source_message_id: int, request_row, parsed
    ) -> ServiceOutcome:
        manager_id = int(self.ctx.settings.manager_telegram_id)
        request_id = int(request_row["id"])
        report_type = request_row["report_type"] or "summary"
        today = local_date(self.ctx.now(), self.ctx.timezone)

        try:
            period = self._period_from_parsed(parsed, today)
        except reporting.PeriodError as exc:
            with self.ctx.db.connection() as connection:
                prompts = repository.count_report_prompts(connection, request_id)
            if prompts >= 2:
                with self.ctx.db.transaction() as connection:
                    repository.update_report_request(
                        connection,
                        request_id=request_id,
                        status="expired",
                        now=self.ctx.now_iso(),
                    )
                self.notifier.send(manager_id, messages.REPORT_PERIOD_GIVEUP, kind="notice")
                return ServiceOutcome(action="expired", note=exc.reason)
            self._ask_for_period(request_id, manager_id, messages.REPORT_PERIOD_QUESTION, report_type)
            return ServiceOutcome(action="awaiting_period", note=exc.reason)

        return self._start_on_demand(
            request_id=request_id,
            manager_id=manager_id,
            period=period,
            report_type=report_type,
        )

    def _ask_for_period(
        self, request_id: int, manager_id: int, question: str, report_type: str
    ) -> None:
        now_iso = self.ctx.now_iso()
        with self.ctx.db.transaction() as connection:
            repository.update_report_request(
                connection,
                request_id=request_id,
                status="awaiting_period",
                report_type=report_type,
                period_kind="ambiguous",
                now=now_iso,
            )
        self.notifier.send(
            manager_id,
            question,
            kind="report_prompt",
            report_request_id=request_id,
        )

    def _start_on_demand(
        self, *, request_id: int, manager_id: int, period: reporting.Period, report_type: str
    ) -> ServiceOutcome:
        now_iso = self.ctx.now_iso()
        report_key = "ondemand:%d" % request_id
        with self.ctx.db.transaction() as connection:
            repository.update_report_request(
                connection,
                request_id=request_id,
                status="resolved",
                report_type=report_type,
                period_kind="resolved",
                now=now_iso,
                resolved=True,
            )
            row, _created = repository.ensure_report_delivery(
                connection,
                report_key=report_key,
                report_type="on_demand",
                variant=report_type,
                manager_telegram_user_id=manager_id,
                period_start_date=period.start_iso,
                period_end_date=period.end_iso,
                scheduled_at_utc=now_iso,
                now=now_iso,
            )
            delivery_id = int(row["id"])
            repository.update_report_request(
                connection,
                request_id=request_id,
                status="resolved",
                report_delivery_id=delivery_id,
                now=now_iso,
            )
        outcome = self._deliver(delivery_id, self.ctx.now())
        if outcome in ("failed", "exhausted"):
            self.notifier.send(manager_id, messages.REPORT_FAILED, kind="notice")
        return ServiceOutcome(
            action="report_" + outcome, delivery_id=delivery_id
        )

    def _period_from_parsed(self, parsed, today: date) -> reporting.Period:
        kind = parsed.period_kind or "ambiguous"
        max_days = self.ctx.settings.report_max_range_days
        if kind in ("today", "yesterday", "last_7_days", "this_week"):
            return reporting.resolve_relative_period(kind, today=today)
        if kind == "explicit_date":
            start = _parse(parsed.start_date)
            end = _parse(parsed.end_date) if parsed.end_date else start
            return reporting.resolve_explicit_period(
                start=start, end=end, today=today, max_days=max_days
            )
        if kind == "range":
            return reporting.resolve_explicit_period(
                start=_parse(parsed.start_date),
                end=_parse(parsed.end_date),
                today=today,
                max_days=max_days,
            )
        raise reporting.PeriodError("ambiguous_period")


def _parse(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return parse_business_date(value)
    except Exception:
        raise reporting.PeriodError("invalid_date")


def _message_ids(row) -> List[int]:
    raw = row["telegram_message_ids"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [int(value) for value in parsed if isinstance(value, int)]


__all__ = ["ReportService", "REPORT_MAX_ATTEMPTS"]
