"""TEST-PLAN F: daily schedule, delivery safety, on-demand reports."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from kalampeach.ai.types import AIUnavailable
from kalampeach.services import messages, reporting
from kalampeach.services.reports import ReportService
from kalampeach.telegram.client import TelegramError, chunk_text
from conftest import (
    EMPLOYEE_ID,
    GROUP_ID,
    MANAGER_ID,
    narrative,
    non_business_extraction,
    report_request,
)
from helpers import (
    bot_messages,
    enqueue,
    fetch_all,
    fetch_one,
    manager_update,
    process,
    private_update,
    report_deliveries,
    run_worker,
    seed_event,
)


def at(ctx, iso: str) -> None:
    ctx.clock.set(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def reports_now(ctx):
    return ReportService(ctx).run_due()


def deliveries(ctx):
    return report_deliveries(ctx)


def test_daily_report_is_not_sent_before_the_due_time(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    summary = reports_now(ctx)  # 15:30 Tehran

    assert summary["daily_created"] == 0
    assert deliveries(ctx) == []
    assert fake_telegram.texts_to(MANAGER_ID) == []


def test_daily_report_is_sent_once_at_the_due_time(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")  # 23:30:05 Tehran

    first = reports_now(ctx)
    assert first["daily_created"] == 1
    rows = deliveries(ctx)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["report_key"] == "daily:9999:2026-11-23"
    assert rows[0]["period_start_date"] == "2026-11-23"
    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert "گزارش روزانه" in text
    assert "1,000" in text

    # A second cron run the same evening must not duplicate anything.
    at(ctx, "2026-11-23T20:05:00Z")
    second = reports_now(ctx)
    assert second["daily_created"] == 0
    assert len(deliveries(ctx)) == 1
    assert len(fake_telegram.texts_to(MANAGER_ID)) == 1


def test_daily_report_is_only_for_the_manager(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    reports_now(ctx)

    assert fake_telegram.texts_to(EMPLOYEE_ID) == []
    assert fake_telegram.texts_to(GROUP_ID) == []
    assert len(fake_telegram.texts_to(MANAGER_ID)) == 1


def test_daily_report_reports_an_empty_day_honestly(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000", event_date="2026-11-20")
    at(ctx, "2026-11-23T20:00:05Z")
    reports_now(ctx)

    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert "هیچ رویدادی ثبت نشده است" in text
    assert "1,000" not in text, "another day's totals must not leak into today"


def test_missed_due_time_is_recovered_once_inside_the_window(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-24T00:30:00Z")  # 04:00 Tehran, 4.5h late

    summary = reports_now(ctx)
    assert summary["daily_created"] == 1
    row = deliveries(ctx)[0]
    assert row["status"] == "sent"
    assert row["period_start_date"] == "2026-11-23"
    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert messages.LATE_REPORT_NOTE in text


def test_missed_day_outside_the_window_is_recorded_as_skipped(ctx):
    seed_event(ctx, 1, amount="1000", event_date="2026-11-23")
    at(ctx, "2026-11-25T12:00:00Z")  # yesterday's window is long gone

    reports_now(ctx)
    rows = deliveries(ctx)
    assert [row["status"] for row in rows] == ["skipped"]
    assert rows[0]["period_start_date"] == "2026-11-24"
    # Nothing is sent for a day the bot was not running.
    assert fetch_one(
        ctx, "SELECT COUNT(*) AS total FROM report_deliveries WHERE status='sent'"
    )["total"] == 0


def test_fresh_install_does_not_report_a_missed_day(ctx):
    at(ctx, "2026-11-25T12:00:00Z")
    summary = reports_now(ctx)
    assert summary["daily_created"] == 0
    assert deliveries(ctx) == []


def test_first_run_after_install_still_sends_todays_report(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    reports_now(ctx)
    assert deliveries(ctx)[0]["status"] == "sent"


def test_long_report_is_split_into_ordered_chunks(ctx, fake_telegram):
    for index in range(12):
        seed_event(
            ctx,
            index + 1,
            event_type="operational_note",
            amount=None,
            currency=None,
            description="یادداشت %d: %s" % (index + 1, "توضیح طولانی " * 30),
        )
    at(ctx, "2026-11-23T20:00:05Z")
    reports_now(ctx)

    row = deliveries(ctx)[0]
    sent = fake_telegram.texts_to(MANAGER_ID)
    assert row["status"] == "sent"
    assert row["chunk_count"] == len(sent) > 1
    assert row["chunks_sent"] == len(sent)
    assert all(0 < len(part) <= 4096 for part in sent)
    assert sent[0].startswith("📊")


def test_transient_send_failure_is_retried_then_completed(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    fake_telegram.queue_failure(TelegramError("telegram_unavailable", retryable=True))
    at(ctx, "2026-11-23T20:00:05Z")

    first = reports_now(ctx)
    assert first["deliveries"] == {"retry": 1}
    row = deliveries(ctx)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1

    at(ctx, "2026-11-23T20:06:00Z")
    second = reports_now(ctx)
    assert second["deliveries"] == {"sent": 1}
    assert deliveries(ctx)[0]["status"] == "sent"
    assert len(fake_telegram.texts_to(MANAGER_ID)) == 1


def test_uncertain_delivery_is_never_resent(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    fake_telegram.queue_failure(TelegramError("timeout", uncertain=True))
    at(ctx, "2026-11-23T20:00:05Z")

    reports_now(ctx)
    assert deliveries(ctx)[0]["status"] == "failed_uncertain"

    at(ctx, "2026-11-23T21:00:00Z")
    summary = reports_now(ctx)
    # Terminal status: it is no longer selected for delivery, so nothing is resent.
    assert summary["deliveries"] == {}
    assert len(fake_telegram.attempts) == 1


def test_resend_resumes_after_the_last_confirmed_chunk(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    with ctx.db.connection() as connection:
        facts = reporting.compute_facts(
            connection,
            period=reporting.Period(date(2026, 11, 23), date(2026, 11, 23)),
            timezone_name="Asia/Tehran",
            generated_at_utc=ctx.now_iso(),
        )
    text = reporting.format_report(facts, title="گزارش روزانه") + "\n\n" + ("خط تکمیلی\n" * 1200)
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    with ctx.db.transaction() as connection:
        connection.execute(
            "INSERT INTO report_deliveries (report_key, report_type, variant, "
            "manager_telegram_user_id, period_start_date, period_end_date, "
            "scheduled_at_utc, status, attempts, chunk_count, chunks_sent, "
            "telegram_message_ids, snapshot_json, created_at_utc, updated_at_utc) "
            "VALUES ('daily:9999:2026-11-23','daily','daily',9999,"
            "'2026-11-23','2026-11-23','2026-11-23T20:00:00Z','pending',1,?,1,"
            "'[1234]', ?, '2026-11-23T20:00:06Z','2026-11-23T20:00:06Z')",
            (
                len(chunks),
                json.dumps(
                    {"facts": facts, "text": text, "narrative_used": False},
                    ensure_ascii=False,
                ),
            ),
        )

    summary = reports_now(ctx)
    row = deliveries(ctx)[0]
    assert row["status"] == "sent"
    assert row["chunks_sent"] == len(chunks)
    # Only the missing chunks are sent; the delivered prefix is never repeated.
    assert len(fake_telegram.attempts) == len(chunks) - 1
    assert summary["deliveries"] == {"sent": 1}


def test_report_delivery_attempts_are_capped(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    fake_telegram.queue_failure(TelegramError("down", retryable=True))
    reports_now(ctx)
    fake_telegram.queue_failure(TelegramError("down", retryable=True))
    at(ctx, "2026-11-23T20:10:00Z")
    reports_now(ctx)
    fake_telegram.queue_failure(TelegramError("down", retryable=True))
    at(ctx, "2026-11-23T20:20:00Z")
    reports_now(ctx)

    row = deliveries(ctx)[0]
    assert row["attempts"] == 3
    assert row["status"] == "pending"

    at(ctx, "2026-11-23T21:00:00Z")
    summary = reports_now(ctx)
    assert summary["deliveries"] == {"exhausted": 1}
    row = deliveries(ctx)[0]
    assert row["attempts"] == 3
    assert row["status"] == "failed"
    assert row["last_error_category"] == "retry_exhausted"


def test_on_demand_report_for_the_manager(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="2500")
    fake_ai.report_requests.append(report_request(report_type="sales", period_kind="today"))

    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش فروش امروز"))

    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert "گزارش" in text and "2,500" in text
    request = fetch_one(ctx, "SELECT * FROM report_requests")
    assert request["status"] == "resolved"
    assert request["report_type"] == "sales"
    delivery = deliveries(ctx)[0]
    assert delivery["report_key"] == "ondemand:%d" % request["id"]
    assert delivery["status"] == "sent"


def test_on_demand_report_is_idempotent_per_message(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="2500")
    fake_ai.report_requests.append(report_request(report_type="summary", period_kind="today"))
    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش"))

    enqueue(ctx, manager_update(update_id=1, message_id=10, text="گزارش"))
    run_worker(ctx)

    assert len(deliveries(ctx)) == 1
    assert len(fetch_all(ctx, "SELECT * FROM report_requests")) == 1
    assert len(fake_telegram.texts_to(MANAGER_ID)) == 1


def test_employee_cannot_request_a_report(ctx, fake_ai):
    fake_ai.extractions.append(non_business_extraction())
    process(ctx, private_update(update_id=1, message_id=10, text="گزارش امروز بده"))

    assert deliveries(ctx) == []
    assert fake_ai.calls == ["extract_event"], "employees never reach the report parser"


def test_employee_private_cannot_be_used_to_read_totals(ctx, fake_ai):
    seed_event(ctx, 1, amount="2500")
    fake_ai.extractions.append(non_business_extraction())
    process(ctx, private_update(update_id=1, message_id=10, text="چقدر فروش داشتیم؟"))
    assert deliveries(ctx) == []


def test_group_manager_request_produces_no_report(ctx, fake_ai, fake_telegram):
    # The manager is not on the recording allowlist and reports are private-only,
    # so a group message is ignored without calling the model or posting anything.
    process(
        ctx,
        manager_update(
            update_id=1, message_id=10, text="گزارش", chat_id=GROUP_ID, chat_type="group"
        ),
    )
    assert deliveries(ctx) == []
    assert fake_ai.calls == []
    assert fake_telegram.attempts == []
    assert fetch_one(ctx, "SELECT COUNT(*) AS total FROM report_requests")["total"] == 0


def test_ambiguous_period_asks_then_resolves(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="700")
    fake_ai.report_requests.append(report_request(period_kind="ambiguous"))
    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش بده"))

    prompt = fake_telegram.texts_to(MANAGER_ID)[-1]
    assert prompt in (messages.REPORT_PERIOD_QUESTION,)
    assert deliveries(ctx) == []

    prompt_id = bot_messages(ctx, "report_prompt")[0]["message_id"]
    fake_ai.report_requests.append(report_request(report_type="summary", period_kind="today"))
    process(
        ctx,
        manager_update(
            update_id=2, message_id=11, text="امروز", reply_to=prompt_id
        ),
    )

    assert deliveries(ctx)[0]["status"] == "sent"
    assert fetch_one(ctx, "SELECT * FROM report_requests")["status"] == "resolved"


def test_unsafe_narrative_falls_back_to_the_deterministic_template(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    fake_ai.report_requests.append(report_request(report_type="summary", period_kind="today"))
    fake_ai.narratives.append(narrative("امروز ۹۹۹٬۹۹۹ تومان فروش داشتیم و سود خوبی کردیم."))

    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش"))

    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert "999,999" not in text and "۹۹۹٬۹۹۹" not in text
    assert "سود خوبی" not in text
    assert "1,000" in text
    assert deliveries(ctx)[0]["narrative_used"] == 0


def test_narrative_failure_does_not_block_the_report(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    fake_ai.report_requests.append(report_request(report_type="summary", period_kind="today"))
    fake_ai.always_fail["write_narrative"] = AIUnavailable("down")

    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش"))

    assert deliveries(ctx)[0]["status"] == "sent"
    assert "1,000" in "\n".join(fake_telegram.texts_to(MANAGER_ID))


def test_safe_narrative_is_included(ctx, fake_ai, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    fake_ai.report_requests.append(report_request(report_type="summary", period_kind="today"))
    fake_ai.narratives.append(narrative("امروز 1,000 فروش ثبت شد."))

    process(ctx, manager_update(update_id=1, message_id=10, text="گزارش"))

    text = "\n".join(fake_telegram.texts_to(MANAGER_ID))
    assert "امروز 1,000 فروش ثبت شد." in text
    assert deliveries(ctx)[0]["narrative_used"] == 1


def test_report_text_respects_chunk_limit():
    text = "خط\n\n" * 3000
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "").strip()[: len(text.replace("\n", "").strip())]
