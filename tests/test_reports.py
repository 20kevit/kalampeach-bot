"""Deterministic reports, currency separation and the 23:30 Tehran schedule (F)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.services.reporting import due_daily_dates, send_daily_report
from app.services.worker import run_worker
from app.timeutil import to_utc_iso
from tests.helpers import (
    EMPLOYEE_ID,
    FIXED_NOW,
    GROUP_ID,
    MANAGER_ID,
    build_context,
    enqueue,
    make_config,
    private_update,
    sale_event,
)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


_MESSAGE_IDS = iter(range(10_000, 20_000))


def seed_event(
    ctx,
    *,
    event_type: str = "sale_revenue",
    amount: str | None = "1500000",
    currency: str | None = "تومان",
    event_date: str = "2025-12-23",
    confirmed_at: str = "2025-12-23T19:00:00Z",
    description: str = "seed",
    item_name: str | None = None,
    quantity: str | None = None,
    unit: str | None = None,
    direction: str | None = None,
    status: str = "confirmed",
    message_id: int | None = None,
    actor: int = EMPLOYEE_ID,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
) -> int:
    from app.repo import events, messages

    if message_id is None:
        message_id = next(_MESSAGE_IDS)
    with ctx.db.session() as conn:
        source, _ = messages.get_or_create(
            conn,
            telegram_update_id=None,
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=None,
            sender_telegram_user_id=actor,
            chat_type=chat_type,
            message_text="seed",
            message_timestamp_utc=confirmed_at,
            processing_status="processed",
            now_iso=confirmed_at,
        )
        return events.create(
            conn,
            fields={
                "event_type": event_type,
                "event_date": event_date,
                "amount": amount,
                "currency": currency,
                "item_name": item_name,
                "quantity": quantity,
                "unit": unit,
                "inventory_direction": direction,
                "description": description,
                "notes": None,
            },
            details=None,
            status=status,
            actor_telegram_user_id=actor,
            source_message_id=int(source["id"]),
            now_iso=confirmed_at,
        )


def _run_reports(ctx, when: datetime):
    with ctx.db.session() as conn:
        return run_worker(ctx, conn, now=when, collect_reports=True)


def _report_rows(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM report_deliveries ORDER BY id")]


def _last_manager_message(ctx):
    messages = ctx.telegram.messages_to(MANAGER_ID)
    assert messages, "expected a report to the manager"
    return messages[-1]["text"]


# ------------------------------------------------------------------- schedule
def test_report_schedule_is_computed_in_tehran_not_server_time():
    config = make_config("/tmp/unused")
    before_cutoff = utc("2025-12-23T19:59:00Z")  # 23:29 Tehran
    assert due_daily_dates(before_cutoff, config) == [date(2025, 12, 22)]

    after_cutoff = utc("2025-12-23T20:01:00Z")  # 23:31 Tehran
    assert due_daily_dates(after_cutoff, config) == [date(2025, 12, 22), date(2025, 12, 23)]


def test_catchup_window_bounds_recovery():
    from tests.helpers import make_config as build

    config = build("/tmp/unused", REPORT_CATCHUP_DAYS="0")
    assert due_daily_dates(utc("2025-12-23T09:00:00Z"), config) == []
    assert due_daily_dates(utc("2025-12-23T20:05:00Z"), config) == [date(2025, 12, 23)]


# ---------------------------------------------------------------------- daily
def test_daily_report_is_sent_once_at_2330_tehran(ctx):
    seed_event(ctx, event_type="sale_revenue", amount="1500000")
    seed_event(ctx, event_type="expense_purchase", amount="400000", message_id=2)
    when = utc("2025-12-23T20:05:00Z")  # 23:35 Tehran, slightly late but same day

    _run_reports(ctx, when)
    rows = _report_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["report_key"] == f"daily:2025-12-23:{MANAGER_ID}"
    assert rows[0]["status"] == "sent"
    assert rows[0]["attempts"] == 1

    text = _last_manager_message(ctx)
    assert "1,500,000" in text
    assert "400,000" in text
    assert ctx.telegram.messages_to(GROUP_ID) == []

    # A second run in the same minute must not send a duplicate.
    _run_reports(ctx, utc("2025-12-23T20:06:00Z"))
    assert len(_report_rows(ctx)) == 1
    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 1


def test_missed_tick_is_recovered_once_and_marked_late(ctx):
    seed_event(ctx, event_date="2025-12-22", confirmed_at="2025-12-22T18:00:00Z")
    when = utc("2025-12-23T05:30:00Z")  # 09:00 Tehran next morning

    _run_reports(ctx, when)
    rows = _report_rows(ctx)
    assert [row["report_key"] for row in rows] == [f"daily:2025-12-22:{MANAGER_ID}"]
    assert rows[0]["status"] == "sent"
    text = _last_manager_message(ctx)
    assert "تأخیر" in text
    assert "2025-12-22" in text

    _run_reports(ctx, utc("2025-12-23T05:40:00Z"))
    assert len(_report_rows(ctx)) == 1
    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 1


def test_events_confirmed_after_the_cutoff_are_excluded(ctx):
    seed_event(ctx, amount="100", confirmed_at="2025-12-23T19:00:00Z")
    seed_event(ctx, amount="900", confirmed_at="2025-12-23T21:00:00Z")  # 00:30 Tehran next day
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert "100" in text
    assert "900" not in text


# ------------------------------------------------------------------ arithmetic
def test_sales_and_expenses_are_reported_separately(ctx):
    seed_event(ctx, event_type="sale_revenue", amount="1000")
    seed_event(ctx, event_type="expense_purchase", amount="250", message_id=2)
    with ctx.db.session() as conn:
        from app.services.report_facts import build_facts

        facts = build_facts(
            conn,
            ctx.config,
            start_date=date(2025, 12, 23),
            end_date=date(2025, 12, 23),
            report_type="daily",
            cutoff_utc=None,
            generated_at=FIXED_NOW,
        )
    assert facts["sales"] == [{"currency": "تومان", "total": "1000", "count": 1}]
    assert facts["expenses"] == [{"currency": "تومان", "total": "250", "count": 1}]
    assert facts["difference"] == [
        {"currency": "تومان", "amount": "750", "label": "difference_of_recorded_amounts"}
    ]


def test_different_currencies_are_never_added_together(ctx):
    seed_event(ctx, amount="1000", currency="تومان")
    seed_event(ctx, amount="20", currency="USD", message_id=2)
    with ctx.db.session() as conn:
        from app.services.report_facts import build_facts

        facts = build_facts(
            conn,
            ctx.config,
            start_date=date(2025, 12, 23),
            end_date=date(2025, 12, 23),
            report_type="daily",
            cutoff_utc=None,
            generated_at=FIXED_NOW,
        )
    assert [(entry["currency"], entry["total"]) for entry in facts["sales"]] == [
        ("USD", "20"),
        ("تومان", "1000"),
    ]
    assert facts["difference"] == []  # expenses exist in no currency


def test_decimal_amounts_are_summed_exactly(ctx):
    seed_event(ctx, amount="0.1", currency="USD")
    seed_event(ctx, amount="0.2", currency="USD", message_id=2)
    with ctx.db.session() as conn:
        from app.services.report_facts import build_facts

        facts = build_facts(
            conn,
            ctx.config,
            start_date=date(2025, 12, 23),
            end_date=date(2025, 12, 23),
            report_type="daily",
            cutoff_utc=None,
            generated_at=FIXED_NOW,
        )
    assert facts["sales"][0]["total"] == "0.3"


def test_pending_events_are_excluded_and_disclosed(ctx):
    seed_event(ctx, amount="1000")
    seed_event(ctx, status="pending_clarification", amount=None, currency=None, message_id=2)
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert "1,000" in text
    assert "در انتظار شفاف‌سازی" in text
    with ctx.db.session() as conn:
        row = conn.execute("SELECT status FROM events WHERE status = 'pending_clarification'").fetchone()
    assert row is not None


def test_empty_day_says_no_events_were_recorded(ctx):
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert "هیچ رویدادی ثبت نشده" in text
    assert "نبود فعالیت کاری" in text
    assert "فروش/درآمد ثبت‌شده" not in text


def test_inventory_is_grouped_by_item_unit_and_direction(ctx):
    seed_event(
        ctx,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="آرد",
        quantity="20",
        unit="کیلوگرم",
        direction="received",
    )
    seed_event(
        ctx,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="آرد",
        quantity="5",
        unit="کیلوگرم",
        direction="received",
        message_id=2,
    )
    with ctx.db.session() as conn:
        from app.services.report_facts import build_facts

        facts = build_facts(
            conn,
            ctx.config,
            start_date=date(2025, 12, 23),
            end_date=date(2025, 12, 23),
            report_type="daily",
            cutoff_utc=None,
            generated_at=FIXED_NOW,
        )
    assert facts["inventory"] == [
        {
            "direction": "received",
            "item_name": "آرد",
            "unit": "کیلوگرم",
            "quantity_total": "25",
            "count": 2,
        }
    ]


# --------------------------------------------------------------------- AI use
def test_ai_narrative_is_prepended_without_changing_figures(ctx):
    seed_event(ctx, amount="1500000")
    ctx.ai.queue_narrative("امروز یک فروش ثبت شد.")
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert text.startswith("خلاصه: امروز یک فروش ثبت شد.")
    assert "1,500,000" in text
    rows = _report_rows(ctx)
    assert rows[0]["narrative_source"] == "ai"


def test_narrative_with_invented_number_is_rejected(ctx):
    seed_event(ctx, amount="1500000")
    ctx.ai.queue_narrative("فروش ۹۹۹٬۹۹۹٬۹۹۹ تومان بود.")
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert "999" not in text
    assert not text.startswith("خلاصه:")
    assert "1,500,000" in text
    assert _report_rows(ctx)[0]["narrative_source"] == "template_ai_rejected"


def test_narrative_claiming_profit_is_rejected(ctx):
    seed_event(ctx, amount="1000")
    seed_event(ctx, event_type="expense_purchase", amount="250", message_id=2)
    ctx.ai.queue_narrative("سود امروز خوب بود.")
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    assert not _last_manager_message(ctx).startswith("خلاصه:")
    assert _report_rows(ctx)[0]["narrative_source"] == "template_ai_rejected"


def test_report_is_still_sent_when_ai_is_unavailable(ctx):
    seed_event(ctx, amount="1500000")
    from app.errors import AIUnavailable

    ctx.ai.narrative_error = AIUnavailable("provider down")
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    text = _last_manager_message(ctx)
    assert "1,500,000" in text
    assert _report_rows(ctx)[0]["status"] == "sent"
    assert _report_rows(ctx)[0]["narrative_source"] == "template_ai_unavailable"


# ---------------------------------------------------------------- idempotency
def test_a_live_report_lease_blocks_a_second_sender(ctx):
    seed_event(ctx, amount="1000")
    day = date(2025, 12, 23)
    when = utc("2025-12-23T20:05:00Z")
    from app.repo import reports

    key = f"daily:{day.isoformat()}:{MANAGER_ID}"
    with ctx.db.session() as conn:
        reports.ensure(
            conn,
            report_key=key,
            report_type="daily",
            manager_telegram_user_id=MANAGER_ID,
            period_start_date=day.isoformat(),
            period_end_date=day.isoformat(),
            now_iso=to_utc_iso(when),
        )
        first = reports.claim(
            conn,
            report_key=key,
            token="worker-a",
            now_iso=to_utc_iso(when),
            lease_until_iso=to_utc_iso(when.replace(minute=15)),
            max_attempts=5,
        )
        second = reports.claim(
            conn,
            report_key=key,
            token="worker-b",
            now_iso=to_utc_iso(when),
            lease_until_iso=to_utc_iso(when.replace(minute=15)),
            max_attempts=5,
        )
    assert first is not None and second is None
    with ctx.db.session() as conn:
        assert send_daily_report.__name__  # imported for clarity


def test_uncertain_delivery_is_not_retried_automatically(ctx):
    from app.errors import DeliveryUncertain

    seed_event(ctx, amount="1000")
    ctx.telegram.queue_failure(DeliveryUncertain("timeout after send"))
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    rows = _report_rows(ctx)
    assert rows[0]["status"] == "uncertain"

    _run_reports(ctx, utc("2025-12-23T20:10:00Z"))
    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 0
    assert _report_rows(ctx)[0]["status"] == "uncertain"


def test_retryable_delivery_failure_is_retried_and_eventually_sent(ctx):
    from app.errors import TelegramUnavailable

    seed_event(ctx, amount="1000")
    ctx.telegram.queue_failure(TelegramUnavailable("telegram 502"))
    first = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, first)
    row = _report_rows(ctx)[0]
    assert row["status"] == "failed"
    assert row["last_error_category"] == "TELEGRAM_UNAVAILABLE"
    assert row["next_attempt_at_utc"] is not None

    _run_reports(ctx, utc("2025-12-23T20:30:00Z"))
    assert _report_rows(ctx)[0]["status"] == "sent"
    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 1


def test_report_is_traceable_to_its_facts(ctx):
    seed_event(ctx, amount="1500000")
    when = utc("2025-12-23T20:05:00Z")
    _run_reports(ctx, when)
    row = _report_rows(ctx)[0]
    assert row["fact_snapshot_json"]
    assert '"total":"1500000"' in row["fact_snapshot_json"].replace(" ", "")
    assert row["telegram_message_id"] is not None
    assert row["sent_at_utc"] is not None


def test_manager_report_is_never_broadcast_to_the_group(ctx):
    seed_event(ctx, amount="1500000")
    _run_reports(ctx, utc("2025-12-23T20:05:00Z"))
    assert ctx.telegram.messages_to(GROUP_ID) == []


# ----------------------------------------------------------------- on demand
def test_manager_on_demand_report_honours_the_requested_period(ctx, fake_ai):
    seed_event(ctx, amount="1000", event_date="2025-12-20", confirmed_at="2025-12-20T10:00:00Z")
    seed_event(ctx, amount="2000", event_date="2025-12-21", confirmed_at="2025-12-21T10:00:00Z", message_id=2)
    fake_ai.queue_intent(
        intent="report", period_start="2025-12-20", period_end="2025-12-21", focus="sales"
    )
    when = utc("2025-12-23T09:00:00Z")
    enqueue(ctx, private_update(1, "گزارش فروش ۲۰ تا ۲۱ دسامبر", sender_id=MANAGER_ID), now=when)
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=when, collect_reports=False)

    messages = ctx.telegram.messages_to(MANAGER_ID)
    assert len(messages) == 1
    text = messages[0]["text"]
    # Two recorded sales across the requested range are aggregated exactly.
    assert "3,000" in text and "(2 رکورد)" in text
    assert "2025-12-20" in text and "2025-12-21" in text
    rows = _report_rows(ctx)
    assert rows[0]["report_type"] == "on_demand"
    assert rows[0]["status"] == "sent"


def test_ambiguous_report_period_asks_instead_of_guessing(ctx, fake_ai):
    fake_ai.queue_intent(
        intent="report",
        period_start=None,
        period_end=None,
        needs_clarification=True,
        question="کدام بازه را می‌خواهید؟",
    )
    enqueue(ctx, private_update(1, "گزارش بده", sender_id=MANAGER_ID))
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    messages = ctx.telegram.messages_to(MANAGER_ID)
    assert len(messages) == 1
    assert "بازه" in messages[0]["text"]
    assert _report_rows(ctx) == []


def test_over_wide_report_range_is_refused(ctx, fake_ai):
    fake_ai.queue_intent(
        intent="report", period_start="2025-01-01", period_end="2025-02-28", focus="all"
    )
    enqueue(ctx, private_update(1, "گزارش دو ماه اول سال", sender_id=MANAGER_ID))
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    assert "بیش از" in ctx.telegram.messages_to(MANAGER_ID)[-1]["text"]
    assert _report_rows(ctx) == []


def test_duplicate_on_demand_request_sends_one_report(ctx, fake_ai):
    seed_event(ctx, amount="1000")
    fake_ai.queue_intent(intent="report", period_start="2025-12-23", period_end="2025-12-23")
    update = private_update(1, "گزارش امروز", sender_id=MANAGER_ID)
    enqueue(ctx, update)
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    fake_ai.queue_intent(intent="report", period_start="2025-12-23", period_end="2025-12-23")
    enqueue(ctx, {**update, "update_id": 2})
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)

    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 1
    assert len(_report_rows(ctx)) == 1


def test_on_demand_retryable_failure_is_retried_and_delivered_once(ctx, fake_ai):
    from app.errors import TelegramUnavailable

    seed_event(ctx, amount="1000")
    # The update is re-processed on retry, so the intent call happens twice.
    for _ in range(2):
        fake_ai.queue_intent(
            intent="report", period_start="2025-12-23", period_end="2025-12-23"
        )
    when = utc("2025-12-23T09:00:00Z")
    enqueue(ctx, private_update(1, "گزارش امروز", sender_id=MANAGER_ID), now=when)
    ctx.telegram.queue_failure(TelegramUnavailable("telegram 502"))
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=when, collect_reports=False)

    rows = _report_rows(ctx)
    assert rows[0]["status"] == "failed"
    assert ctx.telegram.messages_to(MANAGER_ID) == []

    # The same queued update retries after the backoff and delivers exactly once.
    later = when + timedelta(minutes=20)
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=later, collect_reports=False)
    assert _report_rows(ctx)[0]["status"] == "sent"
    assert len(ctx.telegram.messages_to(MANAGER_ID)) == 1

    with ctx.db.session() as conn:
        row = conn.execute("SELECT status FROM telegram_inbox").fetchone()
    assert row["status"] == "done"


def test_manager_message_classified_as_event_goes_to_capture(tmp_path):
    ctx = build_context(tmp_path, MANAGER_CAN_RECORD_EVENTS="true")
    ctx.ai.queue_intent(intent="event", period_start=None, period_end=None)
    ctx.ai.queue_extraction(sale_event(amount="700"))
    enqueue(ctx, private_update(1, "فروش ۷۰۰ تومان", sender_id=MANAGER_ID))
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert _report_rows(ctx) == []


def test_manager_small_talk_is_not_captured_or_reported(ctx, fake_ai):
    fake_ai.queue_intent(intent="not_business", period_start=None, period_end=None)
    enqueue(ctx, private_update(1, "سلام", sender_id=MANAGER_ID))
    with ctx.db.session() as conn:
        run_worker(ctx, conn, now=FIXED_NOW, collect_reports=False)
    assert ctx.telegram.messages_to(MANAGER_ID) == []
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0] == 0


def test_undated_pending_record_is_disclosed_as_incomplete(ctx):
    seed_event(ctx, amount="1000")
    seed_event(
        ctx,
        status="pending_clarification",
        amount=None,
        currency=None,
        event_date=None,
        message_id=2,
    )
    _run_reports(ctx, utc("2025-12-23T20:05:00Z"))
    text = _last_manager_message(ctx)
    assert "تاریخ مشخصی ندارد" in text
