"""Strict AI output validation (AI-CONTRACT section 3 and 7)."""

from __future__ import annotations

import json

import pytest

from kalampeach.ai.schema import (
    parse_json_object,
    validate_clarification_answer,
    validate_correction,
    validate_extraction,
    validate_narrative,
    validate_report_request,
)
from kalampeach.ai.types import AIInvalidOutput


def extraction_payload(**overrides):
    payload = {
        "classification": "business_event",
        "event_type": "sale_revenue",
        "confidence": 0.9,
        "event_date": None,
        "amount": "1200",
        "currency": "تومان",
        "item_name": None,
        "quantity": None,
        "unit": None,
        "inventory_direction": None,
        "description": "فروش",
        "missing_fields": [],
        "ambiguities": [],
        "reason": "فروش کالا",
    }
    payload.update(overrides)
    return payload


def test_valid_extraction_is_accepted():
    result = validate_extraction(extraction_payload())
    assert result.classification == "business_event"
    assert result.event_type == "sale_revenue"
    assert result.amount == "1200"
    assert result.currency == "تومان"


def test_unknown_keys_are_rejected():
    payload = extraction_payload()
    payload["profit"] = "1000"
    with pytest.raises(AIInvalidOutput):
        validate_extraction(payload)


def test_missing_keys_are_rejected():
    payload = extraction_payload()
    del payload["ambiguities"]
    with pytest.raises(AIInvalidOutput):
        validate_extraction(payload)


def test_amounts_must_not_be_floats():
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(amount=1200.5))


def test_integer_amounts_are_normalized_to_strings():
    result = validate_extraction(extraction_payload(amount=1200))
    assert result.amount == "1200"


def test_invalid_enums_are_rejected():
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(classification="maybe"))
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(event_type="profit"))
    with pytest.raises(AIInvalidOutput):
        validate_extraction(
            extraction_payload(
                event_type="inventory_movement", inventory_direction="sold"
            )
        )


def test_business_event_requires_event_type():
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(event_type=None))


def test_non_business_must_not_carry_event_fields():
    with pytest.raises(AIInvalidOutput):
        validate_extraction(
            extraction_payload(classification="non_business", event_type="sale_revenue")
        )
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(classification="unclear", amount="100"))

    result = validate_extraction(
        extraction_payload(
            classification="non_business",
            event_type=None,
            amount=None,
            currency=None,
            description=None,
        )
    )
    assert result.classification == "non_business"
    assert result.is_business_event is False


def test_dates_must_be_real_iso_dates():
    assert validate_extraction(extraction_payload(event_date="2026-11-23")).event_date == "2026-11-23"
    for bad in ("2026-13-40", "23/11/2026", "1405-09-02", 20261123):
        with pytest.raises(AIInvalidOutput):
            validate_extraction(extraction_payload(event_date=bad))


def test_confidence_bounds():
    for bad in (-0.1, 1.1, "high", True):
        with pytest.raises(AIInvalidOutput):
            validate_extraction(extraction_payload(confidence=bad))


def test_missing_fields_must_use_the_known_vocabulary():
    result = validate_extraction(extraction_payload(missing_fields=["amount", "currency"]))
    assert result.missing_fields == ("amount", "currency")
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(missing_fields=["profit"]))


def test_long_text_is_rejected():
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(currency="x" * 40))
    with pytest.raises(AIInvalidOutput):
        validate_extraction(extraction_payload(description="x" * 1200))


def test_json_parsing_is_strict():
    with pytest.raises(AIInvalidOutput):
        parse_json_object("```json\n{}\n```")
    with pytest.raises(AIInvalidOutput):
        parse_json_object("here is your answer: {}")
    with pytest.raises(AIInvalidOutput):
        parse_json_object("[1, 2, 3]")
    with pytest.raises(AIInvalidOutput):
        parse_json_object("")
    assert parse_json_object('{"a": 1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def correction_payload(**overrides):
    payload = {
        "interpretation": "correction",
        "changes": {"amount": "1500"},
        "reason": "مبلغ اصلاح شد",
        "clarification_question": None,
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_valid_correction():
    result = validate_correction(correction_payload())
    assert result.changes == {"amount": "1500"}
    assert result.is_correction is True


def test_correction_rejects_unknown_fields():
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(changes={"profit": "10"}))
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(changes={"amount": 10.5}))


def test_correction_requires_changes_only_when_interpreting_a_change():
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(changes={}))
    with pytest.raises(AIInvalidOutput):
        validate_correction(
            correction_payload(interpretation="not_a_correction", changes={"amount": "5"})
        )
    result = validate_correction(
        correction_payload(interpretation="not_a_correction", changes={})
    )
    assert result.is_correction is False


def test_correction_enum_validation():
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(interpretation="maybe"))
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(changes={"inventory_direction": "sold"}))
    with pytest.raises(AIInvalidOutput):
        validate_correction(correction_payload(changes={"event_type": "profit"}))


# ---------------------------------------------------------------------------
# Clarification answers
# ---------------------------------------------------------------------------


def answer_payload(**overrides):
    payload = {
        "values": {"amount": "500"},
        "missing_fields": [],
        "ambiguities": [],
        "reason": "مبلغ ذکر شد",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_clarification_answer_is_validated():
    result = validate_clarification_answer(answer_payload())
    assert result.values == {"amount": "500"}
    with pytest.raises(AIInvalidOutput):
        validate_clarification_answer(answer_payload(values={"amount": 5.5}))
    with pytest.raises(AIInvalidOutput):
        validate_clarification_answer(answer_payload(values={"event_type": "sale_revenue"}))
    with pytest.raises(AIInvalidOutput):
        validate_clarification_answer(answer_payload(values={"unknown": "x"}))


def test_clarification_answer_drops_null_values():
    result = validate_clarification_answer(answer_payload(values={"amount": None}))
    assert result.values == {}


# ---------------------------------------------------------------------------
# Report requests
# ---------------------------------------------------------------------------


def request_payload(**overrides):
    payload = {
        "is_report_request": True,
        "report_type": "summary",
        "period_kind": "today",
        "start_date": None,
        "end_date": None,
        "clarification_question": None,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def test_report_request_relative_periods_clear_explicit_dates():
    result = validate_report_request(
        request_payload(period_kind="today", start_date="2026-11-20")
    )
    assert result.start_date is None
    assert result.period_kind == "today"


def test_report_request_explicit_date_requires_start():
    with pytest.raises(AIInvalidOutput):
        validate_report_request(request_payload(period_kind="explicit_date"))
    result = validate_report_request(
        request_payload(period_kind="explicit_date", start_date="2026-11-20")
    )
    assert result.start_date == "2026-11-20"


def test_report_request_range_requires_both_dates():
    with pytest.raises(AIInvalidOutput):
        validate_report_request(
            request_payload(period_kind="range", start_date="2026-11-01")
        )


def test_non_report_output_must_be_empty():
    with pytest.raises(AIInvalidOutput):
        validate_report_request(
            request_payload(is_report_request=False, period_kind="today")
        )
    result = validate_report_request(
        request_payload(
            is_report_request=False, report_type=None, period_kind=None
        )
    )
    assert result.is_report_request is False


def test_report_request_defaults():
    result = validate_report_request(
        request_payload(report_type=None, period_kind=None)
    )
    assert result.report_type == "summary"
    assert result.period_kind == "ambiguous"


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def test_narrative_validation():
    assert validate_narrative({"narrative": "خلاصه"}).narrative == "خلاصه"
    with pytest.raises(AIInvalidOutput):
        validate_narrative({"narrative": "   "})
    with pytest.raises(AIInvalidOutput):
        validate_narrative({"narrative": "```text```"})
    with pytest.raises(AIInvalidOutput):
        validate_narrative({"narrative": "x" * 2000})
    with pytest.raises(AIInvalidOutput):
        validate_narrative({"summary": "wrong key"})
