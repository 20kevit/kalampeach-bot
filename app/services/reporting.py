"""Manager reporting: 23:30 Asia/Tehran daily report and on-demand reports.

Scheduling is computed in application code from the IANA timezone, never from
the server or cron timezone, so a cron job that fires every minute (or late, or
not at all for a while) still produces exactly one report per Tehran day.

Delivery is idempotent through ``report_deliveries.report_key`` plus a database
lease. A timed-out send is recorded as ``uncertain`` and is *not* retried
automatically, because Telegram may already have accepted it.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.errors import (
    AppError,
    DeliveryUncertain,
    TelegramForbidden,
    TelegramUnavailable,
)
from app.logging_setup import get_logger
from app.messages_fa import REPORT_PERIOD_UNKNOWN, REPORT_RANGE_TOO_WIDE
from app.repo import events as events_repo
from app.repo import messages, reports
from app.services import capture
from app.services import outcome as outcomes
from app.services.authorization import Actor
from app.services.outcome import Outcome
from app.services.report_facts import build_facts
from app.services.report_render import compose
from app.telegram.updates import NormalizedMessage
from app.timeutil import (
    local_cutoff_utc,
    local_date,
    local_datetime,
    parse_utc,
    to_utc_iso,
)

logger = get_logger("reporting")

REPORT_TYPE_DAILY = "daily"
REPORT_TYPE_ON_DEMAND = "on_demand"

LATE_THRESHOLD_MINUTES = 10
CLAIM_LEASE_SECONDS = 120
MAX_REPORTS_PER_RUN = 2

FOCUS_TITLES = {
    "sales": "گزارش فروش/درآمد",
    "expenses": "گزارش هزینه/خرید",
    "inventory": "گزارش گردش موجودی",
    "notes": "گزارش یادداشت‌های عملیاتی",
    "all": "گزارش فعالیت ثبت‌شده",
}


def daily_report_key(day: date, ctx: AppContext) -> str:
    return f"daily:{day.isoformat()}:{ctx.config.manager_telegram_id}"


def due_daily_dates(now: datetime, config) -> list[date]:
    """Tehran days whose 23:30 cutoff has passed, oldest first.

    ``REPORT_CATCHUP_DAYS`` bounds how far back a missed run may recover, so a
    long outage cannot flood the manager with weeks of late reports.
    """

    tz = config.timezone
    today = local_date(now, tz)
    hour, minute = config.report_hour_minute
    dates: list[date] = []
    for offset in range(config.report_catchup_days, -1, -1):
        day = today - timedelta(days=offset)
        cutoff = local_cutoff_utc(day, tz, hour, minute)
        if now >= cutoff:
            dates.append(day)
    return dates


def send_daily_report(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    day: date,
    now: datetime,
) -> str:
    config = ctx.config
    key = daily_report_key(day, ctx)
    report_type = REPORT_TYPE_DAILY
    row, _created = reports.ensure(
        conn,
        report_key=key,
        report_type=report_type,
        manager_telegram_user_id=config.manager_telegram_id,
        period_start_date=day.isoformat(),
        period_end_date=day.isoformat(),
        now_iso=to_utc_iso(now),
    )
    status = str(row["status"])
    if status in (reports.STATUS_SENT, reports.STATUS_UNCERTAIN):
        return status

    token = uuid.uuid4().hex
    claimed = reports.claim(
        conn,
        report_key=key,
        token=token,
        now_iso=to_utc_iso(now),
        lease_until_iso=to_utc_iso(now + timedelta(seconds=CLAIM_LEASE_SECONDS)),
        max_attempts=config.report_max_attempts,
    )
    if claimed is None:
        return "not_claimable"

    hour, minute = config.report_hour_minute
    cutoff = local_cutoff_utc(day, config.timezone, hour, minute)
    try:
        facts = build_facts(
            conn,
            config,
            start_date=day,
            end_date=day,
            report_type=report_type,
            cutoff_utc=cutoff,
            generated_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a delivery failure
        reports.mark_failed(
            conn,
            report_key=key,
            token=token,
            now_iso=to_utc_iso(now),
            category="REPORT_FACTS_FAILED",
            next_attempt_iso=to_utc_iso(now + timedelta(minutes=5)),
        )
        logger.error("report_facts_failed", extra={"fields": {"error": type(exc).__name__}})
        return "failed"

    late = now - cutoff > timedelta(minutes=LATE_THRESHOLD_MINUTES)
    text, narrative_source = compose(ctx, facts, title="گزارش روزانه", late=late)
    status, _retryable = _deliver(
        ctx,
        conn,
        key=key,
        token=token,
        text=text,
        facts=facts,
        narrative_source=narrative_source,
        now=now,
    )
    return status


def run_due_reports(
    ctx: AppContext, conn: sqlite3.Connection, *, now: datetime, limit: int = MAX_REPORTS_PER_RUN
) -> dict[str, str]:
    results: dict[str, str] = {}
    today = local_date(now, ctx.config.timezone)
    for day in due_daily_dates(now, ctx.config):
        if len(results) >= limit:
            break
        key = daily_report_key(day, ctx)
        existing = reports.get(conn, key)
        if existing is None and day != today:
            # First run of a catch-up day with nothing recorded: a late "no
            # events" report would be noise, so only real missed data is
            # recovered. Today's report is always attempted at its cutoff.
            counts = events_repo.count_status_between(
                conn, start_date=day.isoformat(), end_date=day.isoformat()
            )
            if counts.get("confirmed", 0) == 0:
                continue
        if existing is not None:
            existing_status = str(existing["status"])
            if existing_status in (reports.STATUS_SENT, reports.STATUS_UNCERTAIN):
                continue
            if (
                existing_status == reports.STATUS_FAILED
                and int(existing["attempts"]) >= ctx.config.report_max_attempts
            ):
                continue
        status = send_daily_report(ctx, conn, day=day, now=now)
        results[day.isoformat()] = status
        if status in {"sent", "failed", "uncertain"}:
            ctx.logger.info(
                "daily_report_attempt",
                extra={"fields": {"tehran_date": day.isoformat(), "result": status}},
            )
    return results


def _deliver(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    key: str,
    token: str,
    text: str,
    facts: dict[str, Any],
    narrative_source: str,
    now: datetime,
) -> tuple[str, bool]:
    import json

    snapshot = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    try:
        result = ctx.telegram.send_message(
            chat_id=ctx.config.manager_telegram_id, text=text
        )
    except DeliveryUncertain:
        reports.mark_uncertain(
            conn,
            report_key=key,
            token=token,
            now_iso=to_utc_iso(now),
            category="DELIVERY_UNCERTAIN",
            fact_snapshot_json=snapshot,
        )
        logger.error(
            "report_delivery_uncertain",
            extra={"fields": {"report_key": key, "action": "operator review required"}},
        )
        return "uncertain", False
    except TelegramForbidden:
        reports.mark_failed(
            conn,
            report_key=key,
            token=token,
            now_iso=to_utc_iso(now),
            category="TELEGRAM_FORBIDDEN",
            next_attempt_iso=to_utc_iso(now + timedelta(hours=1)),
        )
        logger.error(
            "report_delivery_forbidden",
            extra={"fields": {"report_key": key, "action": "manager must start the bot"}},
        )
        return "failed", False
    except TelegramUnavailable as exc:
        attempts = int(_attempts(conn, key))
        backoff = min(60 * (2 ** max(attempts - 1, 0)), 3600)
        reports.mark_failed(
            conn,
            report_key=key,
            token=token,
            now_iso=to_utc_iso(now),
            category=exc.category,
            next_attempt_iso=to_utc_iso(now + timedelta(seconds=backoff)),
        )
        logger.warning(
            "report_delivery_retryable",
            extra={"fields": {"report_key": key, "backoff_seconds": backoff}},
        )
        return "failed", True
    except AppError as exc:
        reports.mark_failed(
            conn,
            report_key=key,
            token=token,
            now_iso=to_utc_iso(now),
            category=exc.category,
            next_attempt_iso=to_utc_iso(now + timedelta(minutes=5)),
        )
        return "failed", True

    reports.mark_sent(
        conn,
        report_key=key,
        token=token,
        now_iso=to_utc_iso(now),
        telegram_message_id=result.message_id,
        fact_snapshot_json=snapshot,
        narrative_source=narrative_source,
    )
    ctx.logger.info(
        "report_sent",
        extra={"fields": {"report_key": key, "narrative_source": narrative_source}},
    )
    return "sent", False


def _attempts(conn: sqlite3.Connection, key: str) -> int:
    row = reports.get(conn, key)
    return int(row["attempts"]) if row else 1


# --------------------------------------------------------------------- on demand
def try_handle_manager_message(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    actor: Actor,
    now: datetime,
) -> Outcome | None:
    """Handle a manager private message.

    Returns ``None`` when the message is a normal event candidate (the caller
    routes it to capture), otherwise the outcome for this update.
    """

    if not message.is_private or not actor.is_manager or not message.has_text:
        return None

    tz = ctx.config.timezone
    today = local_datetime(now, tz).date()
    intent = ctx.ai.interpret_manager_request(
        text=message.text or "", today_local=today.isoformat()
    )

    if intent.intent == "not_business":
        return outcomes.ignored("manager_non_business")
    if intent.intent != "report":
        return None

    source = capture.record_source(
        conn, message=message, status=messages.STATUS_PENDING, now_iso=to_utc_iso(now)
    )
    source_id = int(source["id"])

    if intent.needs_clarification or not intent.period_start or not intent.period_end:
        _ask_period(ctx, message.chat_id, intent.question, conn, source_id)
        return outcomes.done("report_period_asked")

    try:
        start = date.fromisoformat(intent.period_start)
        end = date.fromisoformat(intent.period_end)
    except ValueError:
        _ask_period(ctx, message.chat_id, None, conn, source_id)
        return outcomes.done("report_period_asked")

    if end < start or end > today:
        _ask_period(ctx, message.chat_id, None, conn, source_id)
        return outcomes.done("report_period_asked")
    if (end - start).days + 1 > ctx.config.max_report_range_days:
        _notify(
            ctx,
            message.chat_id,
            REPORT_RANGE_TOO_WIDE.format(days=ctx.config.max_report_range_days),
        )
        return outcomes.done("report_range_too_wide")

    key = f"ondemand:{message.chat_id}:{message.message_id}"
    reports.ensure(
        conn,
        report_key=key,
        report_type=REPORT_TYPE_ON_DEMAND,
        manager_telegram_user_id=actor.telegram_user_id,
        period_start_date=start.isoformat(),
        period_end_date=end.isoformat(),
        now_iso=to_utc_iso(now),
    )
    token = uuid.uuid4().hex
    claimed = reports.claim(
        conn,
        report_key=key,
        token=token,
        now_iso=to_utc_iso(now),
        lease_until_iso=to_utc_iso(now + timedelta(seconds=CLAIM_LEASE_SECONDS)),
        max_attempts=ctx.config.report_max_attempts,
    )
    if claimed is None:
        return _on_demand_not_claimed(ctx, conn, key=key, now=now)

    focus = intent.focus or "all"
    facts = build_facts(
        conn,
        ctx.config,
        start_date=start,
        end_date=end,
        report_type=REPORT_TYPE_ON_DEMAND,
        cutoff_utc=None,
        generated_at=now,
        focus=focus,
    )
    text, narrative_source = compose(
        ctx, facts, title=FOCUS_TITLES.get(focus, FOCUS_TITLES["all"])
    )

    status, retryable = _deliver(
        ctx,
        conn,
        key=key,
        token=token,
        text=text,
        facts=facts,
        narrative_source=narrative_source,
        now=now,
    )
    if status == "sent":
        messages.set_status(conn, source_id, messages.STATUS_PROCESSED)
        return outcomes.done("report_sent")
    if status == "uncertain":
        return outcomes.done("report_uncertain")
    if retryable:
        return outcomes.retry("TELEGRAM_UNAVAILABLE")
    return outcomes.done("report_failed")


def _on_demand_not_claimed(
    ctx: AppContext, conn: sqlite3.Connection, *, key: str, now: datetime
) -> Outcome:
    """Decide what to do when the delivery row could not be claimed.

    Never sends a second copy. A send already in flight, or a retry that is not
    due yet, leaves the update queued instead of silently dropping the report.
    """

    current = reports.get(conn, key)
    if current is None:
        return outcomes.retry("REPORT_STATE_MISSING")
    status = str(current["status"])
    if status in (reports.STATUS_SENT, reports.STATUS_UNCERTAIN):
        return outcomes.done("report_duplicate")
    if status == reports.STATUS_SENDING:
        return outcomes.retry("REPORT_IN_FLIGHT", after_seconds=CLAIM_LEASE_SECONDS)
    if int(current["attempts"]) >= ctx.config.report_max_attempts:
        ctx.logger.error(
            "report_attempts_exhausted",
            extra={"fields": {"report_key": key}},
        )
        return outcomes.done("report_attempts_exhausted")
    next_attempt = parse_utc(current["next_attempt_at_utc"])
    delay = 30
    if next_attempt is not None:
        delay = max(1, min(int((next_attempt - now).total_seconds()), 3600))
    return outcomes.retry("REPORT_DEFERRED", after_seconds=delay)


def _ask_period(
    ctx: AppContext,
    chat_id: int,
    question: str | None,
    conn: sqlite3.Connection,
    source_id: int,
) -> None:
    text = (question or "").strip()
    if not text or len(text) > 400:
        text = REPORT_PERIOD_UNKNOWN
    messages.set_status(conn, source_id, messages.STATUS_CLARIFICATION)
    _notify(ctx, chat_id, text)


def _notify(ctx: AppContext, chat_id: int, text: str) -> None:
    try:
        ctx.telegram.send_message(chat_id=chat_id, text=text)
    except TelegramForbidden:
        logger.info("report_notice_undeliverable", extra={"fields": {"category": "TELEGRAM_FORBIDDEN"}})
    except TelegramUnavailable:
        logger.info("report_notice_failed", extra={"fields": {"category": "TELEGRAM_UNAVAILABLE"}})

