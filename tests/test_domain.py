"""Domain rules: decimal safety, unknown-vs-zero, currency and semantics."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain import events as domain
from app.errors import DomainValidationError
from app.money import AmountError, canonical_decimal, format_decimal, parse_decimal

TODAY = date(2025, 12, 23)


def test_floats_are_rejected_for_money():
    with pytest.raises(AmountError):
        parse_decimal(1500.0, "amount")
    with pytest.raises(AmountError):
        parse_decimal(True, "amount")


def test_negative_and_zero_amounts_are_rejected():
    with pytest.raises(AmountError):
        parse_decimal("-5", "amount")
    with pytest.raises(AmountError):
        parse_decimal("0", "amount")


def test_decimal_strings_are_canonicalised_without_precision_loss():
    assert canonical_decimal(parse_decimal("0.10")) == "0.1"
    assert canonical_decimal(parse_decimal("1500000.50")) == "1500000.5"
    assert canonical_decimal(parse_decimal("1,500,000")) == "1500000"
    assert canonical_decimal(parse_decimal("۱۲۳".translate(str.maketrans("۱۲۳", "123")))) == "123"
    assert format_decimal(Decimal("1500000.5")) == "1,500,000.5"


def test_absurd_precision_is_rejected():
    with pytest.raises(AmountError):
        parse_decimal("1.1234567", "amount")  # more than 6 decimal places


def test_unknown_amount_stays_none():
    fields, details = domain.normalise_fields(
        {"event_type": "sale_revenue", "event_date": "2025-12-23", "amount": None, "currency": "تومان"},
        message_local_date=TODAY,
    )
    assert fields["amount"] is None
    assert domain.missing_required_fields(fields) == ["amount"]


def test_sale_requires_amount_currency_and_date():
    fields, _ = domain.normalise_fields(
        {"event_type": "sale_revenue", "event_date": None, "amount": None, "currency": None},
        message_local_date=TODAY,
    )
    assert domain.missing_required_fields(fields) == ["event_date", "amount", "currency"]
    with pytest.raises(DomainValidationError):
        domain.validate_semantics(fields)


def test_inventory_requires_item_quantity_unit_and_direction():
    fields, _ = domain.normalise_fields(
        {"event_type": "inventory_movement", "event_date": "2025-12-23", "item_name": None,
         "quantity": None, "unit": None, "inventory_direction": None},
        message_local_date=TODAY,
    )
    assert set(domain.missing_required_fields(fields)) == {
        "item_name",
        "quantity",
        "unit",
        "inventory_direction",
    }


def test_operational_note_requires_a_description():
    fields, _ = domain.normalise_fields(
        {"event_type": "operational_note", "event_date": "2025-12-23", "description": "  "},
        message_local_date=TODAY,
    )
    assert domain.missing_required_fields(fields) == ["description"]


def test_inapplicable_fields_are_dropped_and_recorded():
    fields, details = domain.normalise_fields(
        {
            "event_type": "operational_note",
            "event_date": "2025-12-23",
            "description": "تعمیر یخچال",
            "amount": "100",
            "currency": "تومان",
        },
        message_local_date=TODAY,
    )
    assert fields["amount"] is None and fields["currency"] is None
    assert details["dropped_inapplicable_fields"] == ["amount", "currency"]


def test_implausible_dates_are_rejected_not_guessed():
    with pytest.raises(DomainValidationError):
        domain.normalise_fields(
            {"event_type": "sale_revenue", "event_date": "2024-01-01", "amount": "1", "currency": "USD"},
            message_local_date=TODAY,
        )


def test_message_date_may_fill_the_date_only_when_enabled():
    raw = {"event_type": "sale_revenue", "event_date": None, "amount": "1", "currency": "USD"}
    without, _ = domain.normalise_fields(raw, message_local_date=TODAY)
    assert without["event_date"] is None

    with_flag, details = domain.normalise_fields(
        raw, message_local_date=TODAY, allow_message_date_as_event_date=True
    )
    assert with_flag["event_date"] == "2025-12-23"
    assert details["event_date_from_message"] is True


def test_currency_labels_are_preserved_and_never_converted():
    fields, _ = domain.normalise_fields(
        {"event_type": "expense_purchase", "event_date": "2025-12-23", "amount": "10", "currency": " یورو "},
        message_local_date=TODAY,
    )
    assert fields["currency"] == "یورو"


def test_difference_is_computed_with_decimal_not_binary_float():
    a = parse_decimal("0.3")
    b = parse_decimal("0.1")
    assert canonical_decimal(a - b) == "0.2"


def test_snapshot_contains_only_structured_fields():
    fields, _ = domain.normalise_fields(
        {"event_type": "sale_revenue", "event_date": "2025-12-23", "amount": "1", "currency": "USD"},
        message_local_date=TODAY,
    )
    data = domain.snapshot(fields, status="confirmed")
    assert set(data) == set(domain.MUTABLE_FIELDS) | {"status"}
    assert data["status"] == "confirmed"


def test_patch_cannot_target_unknown_fields():
    with pytest.raises(DomainValidationError):
        domain.apply_patch({}, {"status": "rejected"})
