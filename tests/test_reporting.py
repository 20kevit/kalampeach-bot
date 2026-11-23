"""TEST-PLAN F: deterministic totals, currency separation, report wording."""

from __future__ import annotations

from datetime import date

import pytest

from kalampeach import repository
from kalampeach.services import reporting
from kalampeach.timeutil import to_utc_iso
from conftest import EMPLOYEE_ID, GROUP_ID
from helpers import events


def seed(
    ctx,
    seq: int,
    *,
    event_type="sale_revenue",
    amount="1000",
    currency="تومان",
    event_date="2026-11-23",
    status="confirmed",
    item_name=None,
    quantity=None,
    unit=None,
    direction=None,
    description=None,
):
    with ctx.db.transaction() as connection:
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=1000 + seq,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=2000 + seq,
            reply_to_message_id=None,
            sender_telegram_user_id=EMPLOYEE_ID,
            message_text="seed %d" % seq,
            message_timestamp_utc=to_utc_iso(ctx.now()),
            processing_status="processed",
            now=to_utc_iso(ctx.now()),
        )
        repository.upsert_user(
            connection,
            telegram_user_id=EMPLOYEE_ID,
            display_name="کارمند نمونه",
            role="employee",
            now=to_utc_iso(ctx.now()),
        )
        return repository.insert_event(
            connection,
            event_type=event_type,
            status=status,
            event_date=event_date,
            amount=amount,
            currency=currency,
            currency_source="explicit" if currency else None,
            item_name=item_name,
            quantity=quantity,
            unit=unit,
            inventory_direction=direction,
            description=description,
            created_by_telegram_user_id=EMPLOYEE_ID,
            source_message_id=int(source["id"]),
            status_reason=None,
            now=to_utc_iso(ctx.now()),
        )


def facts_for(ctx, start="2026-11-23", end="2026-11-23", report_type="summary"):
    period = reporting.Period(date.fromisoformat(start), date.fromisoformat(end))
    with ctx.db.connection() as connection:
        return reporting.compute_facts(
            connection,
            period=period,
            timezone_name="Asia/Tehran",
            generated_at_utc=ctx.now_iso(),
        )


def test_sales_and_expenses_are_grouped_by_currency(ctx):
    seed(ctx, 1, amount="1000", currency="تومان")
    seed(ctx, 2, amount="2500", currency="تومان")
    seed(ctx, 3, amount="40", currency="USD")
    seed(ctx, 4, event_type="expense_purchase", amount="600", currency="تومان")

    facts = facts_for(ctx)
    assert facts["totals"]["sale_revenue"]["تومان"] == {
        "currency": "تومان",
        "amount": "3500",
        "count": 2,
    }
    assert facts["totals"]["sale_revenue"]["usd"]["amount"] == "40"
    assert facts["totals"]["sale_revenue"]["usd"]["currency"] == "USD"
    assert facts["totals"]["expense_purchase"]["تومان"]["amount"] == "600"
    assert facts["differences"]["تومان"]["amount"] == "2900"


def test_different_currencies_are_never_added_together(ctx):
    seed(ctx, 1, amount="1000", currency="تومان")
    seed(ctx, 2, amount="5", currency="USD")
    facts = facts_for(ctx)

    assert set(facts["totals"]["sale_revenue"].keys()) == {"تومان", "usd"}
    assert {bucket["currency"] for bucket in facts["totals"]["sale_revenue"].values()} == {
        "تومان",
        "USD",
    }
    text = reporting.format_report(facts)
    assert "1,000" in text and "5" in text
    assert "1,005" not in text
    assert "ارزهای مختلف با هم جمع نمی‌شوند" in text


def test_decimal_arithmetic_is_exact(ctx):
    seed(ctx, 1, amount="0.1")
    seed(ctx, 2, amount="0.2")
    facts = facts_for(ctx)
    assert facts["totals"]["sale_revenue"]["تومان"]["amount"] == "0.3"


def test_difference_is_labeled_and_not_called_profit(ctx):
    seed(ctx, 1, amount="1000")
    seed(ctx, 2, event_type="expense_purchase", amount="400")
    facts = facts_for(ctx)
    text = reporting.format_report(facts)
    assert "اختلاف مبالغ ثبت‌شده" in text
    assert "سود حسابداری محسوب نمی‌شود" in text


def test_no_difference_without_both_categories(ctx):
    seed(ctx, 1, amount="1000")
    facts = facts_for(ctx)
    assert facts["differences"] == {}
    assert "اختلاف مبالغ ثبت‌شده" not in reporting.format_report(facts)


def test_pending_events_are_excluded_and_disclosed(ctx):
    seed(ctx, 1, amount="1000")
    seed(ctx, 2, event_type="expense_purchase", amount=None, currency=None, status="pending_clarification")
    seed(ctx, 3, event_type="expense_purchase", amount="500", status="rejected")

    facts = facts_for(ctx)
    text = reporting.format_report(facts)

    assert facts["counts"] == {"confirmed": 1, "pending_clarification": 1, "rejected": 1}
    assert facts["totals"]["expense_purchase"] == {}
    assert "در انتظار شفاف‌سازی: 1" in text
    assert "ردشده: 1" in text


def test_amounts_without_currency_are_excluded_and_disclosed(ctx):
    seed(ctx, 1, amount="1000", currency=None)
    seed(ctx, 2, amount="700", currency="تومان")
    facts = facts_for(ctx)

    assert facts["totals"]["sale_revenue"]["تومان"]["amount"] == "700"
    assert facts["missing_currency"]["sale_revenue"] == 1
    assert "بدون واحد پول" in reporting.format_report(facts)


def test_inventory_movements_are_grouped_by_item_unit_and_direction(ctx):
    seed(
        ctx,
        1,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="رب گوجه",
        quantity="10",
        unit="کیلوگرم",
        direction="consumed",
    )
    seed(
        ctx,
        2,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="رب گوجه",
        quantity="2.5",
        unit="کیلوگرم",
        direction="consumed",
    )
    seed(
        ctx,
        3,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="برنج",
        quantity="4",
        unit="کیسه",
        direction="received",
    )
    # Incomplete inventory row (no unit) must not be summed.
    seed(
        ctx,
        4,
        event_type="inventory_movement",
        amount=None,
        currency=None,
        item_name="نمک",
        quantity="3",
        unit=None,
        direction="consumed",
    )

    facts = facts_for(ctx)
    assert facts["incomplete_inventory"] == 1
    inventory = {(item["direction"], item["item"], item["unit"]): item for item in facts["inventory"]}
    assert inventory[("consumed", "رب گوجه", "کیلوگرم")]["quantity"] == "12.5"
    assert inventory[("consumed", "رب گوجه", "کیلوگرم")]["count"] == 2
    assert inventory[("received", "برنج", "کیسه")]["quantity"] == "4"

    text = reporting.format_report(facts)
    assert "مصرف: رب گوجه — 12.5 کیلوگرم (2 مورد)" in text
    assert "دریافت: برنج — 4 کیسه (1 مورد)" in text


def test_incidents_are_listed_and_capped(ctx):
    for index in range(12):
        seed(
            ctx,
            index + 1,
            event_type="operational_note",
            amount=None,
            currency=None,
            description="یادداشت شماره %d" % (index + 1),
        )
    facts = facts_for(ctx, report_type="incidents")
    assert facts["incidents_total"] == 12
    assert len(facts["incidents"]) == 10
    assert "و 2 مورد دیگر" in reporting.format_report(facts, report_type="incidents")


def test_empty_period_wording(ctx):
    facts = facts_for(ctx)
    text = reporting.format_report(facts)
    assert "هیچ رویدادی ثبت نشده است" in text
    assert "به معنی نبودِ فعالیت کسب‌وکار نیست" in text
    assert "0 تومان" not in text


def test_report_type_sections(ctx):
    seed(ctx, 1, amount="1000")
    seed(ctx, 2, event_type="expense_purchase", amount="300")
    seed(ctx, 3, event_type="operational_note", amount=None, currency=None, description="خرابی یخچال")
    facts = facts_for(ctx)

    sales_only = reporting.format_report(facts, report_type="sales")
    assert "فروش/درآمد ثبت‌شده" in sales_only
    assert "هزینه/خرید ثبت‌شده" not in sales_only
    assert "رخدادها و یادداشت‌های مهم" not in sales_only

    expenses_only = reporting.format_report(facts, report_type="expenses")
    assert "هزینه/خرید ثبت‌شده" in expenses_only
    assert "فروش/درآمد ثبت‌شده" not in expenses_only

    incidents_only = reporting.format_report(facts, report_type="incidents")
    assert "خرابی یخچال" in incidents_only
    assert "فروش/درآمد ثبت‌شده" not in incidents_only


def test_report_includes_author_names_and_date(ctx):
    seed(ctx, 1, amount="1000", description="فروش ویژه")
    text = reporting.format_report(facts_for(ctx))
    assert "2026-11-23" in text
    assert "Asia/Tehran" in text


def test_narrative_guard_rejects_invented_numbers(ctx):
    seed(ctx, 1, amount="1000")
    facts = facts_for(ctx)
    assert reporting.narrative_is_safe("امروز ۱٬۰۰۰ تومان فروش ثبت شد.", facts)
    assert not reporting.narrative_is_safe("امروز ۹۹۹٬۹۹۹ تومان فروش ثبت شد.", facts)
    assert not reporting.narrative_is_safe("فروش 5 مورد بود.", facts)


def test_narrative_guard_rejects_profit_language(ctx):
    seed(ctx, 1, amount="1000")
    facts = facts_for(ctx)
    assert not reporting.narrative_is_safe("سود امروز خوب بود.", facts)
    assert reporting.narrative_is_safe("اختلاف مبالغ ثبت‌شده 1,000 است.", facts)


def test_narrative_guard_rejects_empty_text(ctx):
    facts = facts_for(ctx)
    assert not reporting.narrative_is_safe("   ", facts)


def test_relative_period_resolution():
    monday = date(2026, 11, 23)  # Monday
    assert reporting.resolve_relative_period("today", today=monday) == reporting.Period(
        monday, monday
    )
    assert reporting.resolve_relative_period("yesterday", today=monday).start == date(
        2026, 11, 22
    )
    week = reporting.resolve_relative_period("last_7_days", today=monday)
    assert week.start == date(2026, 11, 17)
    this_week = reporting.resolve_relative_period("this_week", today=monday)
    assert this_week.start == date(2026, 11, 21), "the Iranian week starts on Saturday"
    assert this_week.end == monday


def test_explicit_period_validation():
    today = date(2026, 11, 23)
    period = reporting.resolve_explicit_period(
        start=date(2026, 11, 1), end=date(2026, 11, 10), today=today, max_days=62
    )
    assert period.day_count == 10

    with pytest.raises(reporting.PeriodError):
        reporting.resolve_explicit_period(
            start=date(2026, 11, 10), end=date(2026, 11, 1), today=today, max_days=62
        )
    with pytest.raises(reporting.PeriodError):
        reporting.resolve_explicit_period(
            start=date(2026, 11, 20), end=date(2026, 12, 1), today=today, max_days=62
        )
    with pytest.raises(reporting.PeriodError):
        reporting.resolve_explicit_period(
            start=date(2025, 1, 1), end=today, today=today, max_days=62
        )
    with pytest.raises(reporting.PeriodError):
        reporting.resolve_explicit_period(start=None, end=None, today=today, max_days=62)
