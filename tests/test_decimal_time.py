"""Decimal-safe money handling and UTC/Asia/Tehran time rules (TEST-PLAN G)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from kalampeach.decimal_utils import (
    DecimalValueError,
    decimal_to_storage,
    format_amount,
    is_plain_decimal_string,
    parse_decimal,
)
from kalampeach.timeutil import (
    TimeError,
    local_date,
    local_midnight_utc,
    local_time_to_utc,
    parse_business_date,
    parse_utc_iso,
    to_utc_iso,
    utc_now,
)


# ---------------------------------------------------------------------------
# Decimals
# ---------------------------------------------------------------------------


def test_persian_and_arabic_digits_are_normalized():
    assert parse_decimal("۱۲۰۰", field="amount") == Decimal("1200")
    assert parse_decimal("٢٥٠", field="amount") == Decimal("250")


def test_thousands_separators_are_removed():
    assert parse_decimal("1,250,000", field="amount") == Decimal("1250000")
    assert parse_decimal("۱٬۲۵۰٬۰۰۰", field="amount") == Decimal("1250000")


def test_floats_are_rejected_to_protect_money_precision():
    with pytest.raises(DecimalValueError):
        parse_decimal(0.1, field="amount")
    with pytest.raises(DecimalValueError):
        parse_decimal(True, field="amount")


def test_decimal_precision_is_preserved_without_binary_rounding():
    total = parse_decimal("0.1", field="a") + parse_decimal("0.2", field="b")
    assert total == Decimal("0.3")
    assert format_amount(total) == "0.3"


def test_zero_and_negative_rules():
    assert parse_decimal("0", field="amount") == Decimal("0")
    with pytest.raises(DecimalValueError):
        parse_decimal("0", field="amount", allow_zero=False)
    with pytest.raises(DecimalValueError):
        parse_decimal("-5", field="amount")
    assert parse_decimal("-5", field="amount", allow_negative=True) == Decimal("-5")


def test_too_many_decimal_places_are_rejected():
    with pytest.raises(DecimalValueError):
        parse_decimal("1.123456", field="amount")


def test_non_numeric_values_are_rejected():
    for value in ("", "abc", "12a", "1/2", None, [], {}):
        with pytest.raises(DecimalValueError):
            parse_decimal(value, field="amount")


def test_storage_and_display_formats():
    assert decimal_to_storage(Decimal("1200.500")) == "1200.5"
    assert decimal_to_storage(Decimal("0.0")) == "0"
    assert format_amount(Decimal("1250000")) == "1,250,000"
    assert format_amount(Decimal("-2500.75")) == "-2,500.75"
    assert format_amount(Decimal("0")) == "0"


def test_plain_decimal_string_guard():
    assert is_plain_decimal_string("1200")
    assert is_plain_decimal_string("12.5")
    assert not is_plain_decimal_string("1,200")
    assert not is_plain_decimal_string(" 1200")
    assert not is_plain_decimal_string(1200)
    assert not is_plain_decimal_string("1e5")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_utc_iso_roundtrip_is_canonical():
    moment = datetime(2026, 11, 23, 12, 34, 56, tzinfo=timezone.utc)
    text = to_utc_iso(moment)
    assert text == "2026-11-23T12:34:56Z"
    assert parse_utc_iso(text) == moment


def test_naive_datetime_is_refused():
    with pytest.raises(TimeError):
        to_utc_iso(datetime(2026, 1, 1, 0, 0))


def test_business_date_uses_tehran_not_utc():
    from kalampeach.timeutil import load_timezone

    tehran = load_timezone("Asia/Tehran")
    # 2026-11-23 21:00 UTC is 2026-11-24 00:30 in Tehran.
    moment = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
    assert moment.date() == date(2026, 11, 23)
    assert local_date(moment, tehran) == date(2026, 11, 24)


def test_tehran_has_a_fixed_offset_and_no_dst():
    from kalampeach.timeutil import load_timezone

    tehran = load_timezone("Asia/Tehran")
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tehran)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).astimezone(tehran)
    assert winter.utcoffset() == summer.utcoffset() == timedelta(hours=3, minutes=30)


def test_daily_report_boundaries_are_local():
    from kalampeach.timeutil import load_timezone

    tehran = load_timezone("Asia/Tehran")
    day = date(2026, 11, 23)
    midnight = local_midnight_utc(day, tehran)
    scheduled = local_time_to_utc(day, time(23, 30), tehran)
    assert midnight == datetime(2026, 11, 22, 20, 30, tzinfo=timezone.utc)
    assert scheduled == datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)


def test_business_date_parsing_is_strict():
    assert parse_business_date("2026-11-23") == date(2026, 11, 23)
    for bad in ("2026-13-01", "23-11-2026", "2026-1-1", "", None):
        with pytest.raises(TimeError):
            parse_business_date(bad)


def test_utc_now_is_aware_and_second_precision():
    moment = utc_now()
    assert moment.tzinfo is not None
    assert moment.microsecond == 0
