"""Strict validation of AI output before anything touches the database.

Every validator in this module:

* requires a JSON object with an exact, closed key set (unknown keys rejected);
* rejects floats for money/quantities and requires decimal strings;
* rejects impossible enum values, malformed dates, and ``Null`` misuse;
* returns typed dataclasses so services never see raw model text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional

from ..decimal_utils import is_plain_decimal_string, normalize_digits
from ..timeutil import parse_business_date, TimeError
from .types import (
    AIInvalidOutput,
    ClarificationAnswerResult,
    CorrectionResult,
    ExtractionResult,
    NarrativeResult,
    ReportRequestResult,
)

CLASSIFICATIONS = ("business_event", "non_business", "unclear")
EVENT_TYPES = ("sale_revenue", "expense_purchase", "inventory_movement", "operational_note")
INVENTORY_DIRECTIONS = ("received", "consumed", "wasted", "adjustment")
FIELD_NAMES = (
    "event_type",
    "event_date",
    "amount",
    "currency",
    "item_name",
    "quantity",
    "unit",
    "inventory_direction",
    "description",
)
CORRECTION_INTERPRETATIONS = ("correction", "not_a_correction", "unclear")
REPORT_TYPES = ("summary", "sales", "expenses", "inventory", "incidents", "full")
PERIOD_KINDS = (
    "today",
    "yesterday",
    "explicit_date",
    "range",
    "last_7_days",
    "this_week",
    "ambiguous",
)

EXTRACTION_KEYS = (
    "classification",
    "event_type",
    "confidence",
    "event_date",
    "amount",
    "currency",
    "item_name",
    "quantity",
    "unit",
    "inventory_direction",
    "description",
    "missing_fields",
    "ambiguities",
    "reason",
)
CORRECTION_KEYS = ("interpretation", "changes", "reason", "clarification_question", "confidence")
REPORT_REQUEST_KEYS = (
    "is_report_request",
    "report_type",
    "period_kind",
    "start_date",
    "end_date",
    "clarification_question",
    "confidence",
)
NARRATIVE_KEYS = ("narrative",)
CLARIFICATION_KEYS = ("values", "missing_fields", "ambiguities", "reason", "confidence")
CLARIFICATION_VALUE_KEYS = (
    "event_date",
    "amount",
    "currency",
    "item_name",
    "quantity",
    "unit",
    "inventory_direction",
    "description",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_json_object(raw_text: str) -> Dict[str, Any]:
    """Parse a strict JSON object; markdown fences and prose are rejected."""

    if not isinstance(raw_text, str):
        raise AIInvalidOutput("model output is not text")
    text = raw_text.strip()
    if not text:
        raise AIInvalidOutput("model output is empty")
    if text.startswith("```") or text.endswith("```"):
        raise AIInvalidOutput("model output is wrapped in a code fence")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIInvalidOutput("model output is not valid JSON (line %s)" % exc.lineno) from exc
    if not isinstance(parsed, dict):
        raise AIInvalidOutput("model output must be a JSON object")
    return parsed


def _require_keys(payload: Mapping[str, Any], expected: Iterable[str], operation: str) -> None:
    expected_set = set(expected)
    missing = expected_set - set(payload.keys())
    unknown = set(payload.keys()) - expected_set
    if missing:
        raise AIInvalidOutput("%s output is missing keys: %s" % (operation, ",".join(sorted(missing))))
    if unknown:
        raise AIInvalidOutput(
            "%s output has unknown keys: %s" % (operation, ",".join(sorted(unknown)))
        )


def _optional_text(value: Any, field: str, *, max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIInvalidOutput("%s must be a string or null" % field)
    cleaned = value.replace("\r", " ").replace("\n", " ").strip()
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise AIInvalidOutput("%s is longer than %d characters" % (field, max_length))
    return cleaned


def _decimal_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AIInvalidOutput("%s must not be a boolean" % field)
    if isinstance(value, float):
        raise AIInvalidOutput(
            "%s must be a decimal string; floating-point money is not accepted" % field
        )
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = normalize_digits(value.strip().replace(",", ""))
    else:
        raise AIInvalidOutput("%s must be a decimal string or null" % field)
    if not is_plain_decimal_string(text):
        raise AIInvalidOutput("%s must be a non-negative plain decimal string" % field)
    return text


def _optional_date(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise AIInvalidOutput("%s must be YYYY-MM-DD or null" % field)
    try:
        parsed = parse_business_date(value.strip())
    except TimeError as exc:
        raise AIInvalidOutput("%s is not a real calendar date" % field) from exc
    if not 2000 <= parsed.year <= 2100:
        # A Jalali date such as 1405-09-02 must never be stored as Gregorian.
        raise AIInvalidOutput(
            "%s must be a Gregorian date (the model returned an out-of-range year)" % field
        )
    return parsed.isoformat()


def _enum(value: Any, allowed: Iterable[str], field: str, *, optional: bool) -> Optional[str]:
    if value is None:
        if optional:
            return None
        raise AIInvalidOutput("%s is required" % field)
    if not isinstance(value, str) or value not in allowed:
        raise AIInvalidOutput("%s must be one of: %s" % (field, ", ".join(allowed)))
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AIInvalidOutput("confidence must be a number between 0 and 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise AIInvalidOutput("confidence must be between 0 and 1")
    return number


def _string_list(value: Any, field: str, *, allowed: Optional[Iterable[str]] = None) -> tuple:
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise AIInvalidOutput("%s must be a list of strings" % field)
    result = []
    for item in value:
        if not isinstance(item, str):
            raise AIInvalidOutput("%s must contain only strings" % field)
        cleaned = item.strip()
        if not cleaned:
            continue
        if allowed is not None and cleaned not in set(allowed):
            raise AIInvalidOutput("%s contains an unsupported value: %s" % (field, cleaned))
        if len(cleaned) > 120:
            raise AIInvalidOutput("%s contains an over-long value" % field)
        result.append(cleaned)
    return tuple(dict.fromkeys(result))


def validate_extraction(payload: Mapping[str, Any]) -> ExtractionResult:
    if not isinstance(payload, Mapping):
        raise AIInvalidOutput("extraction output must be an object")
    _require_keys(payload, EXTRACTION_KEYS, "extraction")

    classification = _enum(payload["classification"], CLASSIFICATIONS, "classification", optional=False)
    event_type = _enum(payload["event_type"], EVENT_TYPES, "event_type", optional=True)
    if classification == "business_event" and event_type is None:
        raise AIInvalidOutput("business_event requires an event_type")
    if classification != "business_event" and event_type is not None:
        raise AIInvalidOutput("event_type must be null unless classification is business_event")

    inventory_direction = _enum(
        payload["inventory_direction"], INVENTORY_DIRECTIONS, "inventory_direction", optional=True
    )
    if classification != "business_event" and inventory_direction is not None:
        raise AIInvalidOutput("inventory_direction must be null for non-events")

    result = ExtractionResult(
        classification=classification,
        event_type=event_type,
        confidence=_confidence(payload["confidence"]),
        event_date=_optional_date(payload["event_date"], "event_date"),
        amount=_decimal_text(payload["amount"], "amount"),
        currency=_optional_text(payload["currency"], "currency", max_length=16),
        item_name=_optional_text(payload["item_name"], "item_name", max_length=200),
        quantity=_decimal_text(payload["quantity"], "quantity"),
        unit=_optional_text(payload["unit"], "unit", max_length=24),
        inventory_direction=inventory_direction,
        description=_optional_text(payload["description"], "description", max_length=1000),
        missing_fields=_string_list(payload["missing_fields"], "missing_fields", allowed=FIELD_NAMES),
        ambiguities=_string_list(payload["ambiguities"], "ambiguities"),
        reason=_optional_text(payload["reason"], "reason", max_length=500),
    )

    if result.classification != "business_event":
        if result.amount is not None or result.quantity is not None:
            raise AIInvalidOutput("non-event output must not contain financial values")
    return result


def _field_value(field_name: str, value: Any, *, prefix: str = "") -> Optional[str]:
    """Validate one event field value coming from the model."""

    label = prefix + field_name
    if field_name == "event_type":
        return _enum(value, EVENT_TYPES, label, optional=True)
    if field_name == "event_date":
        return _optional_date(value, label)
    if field_name in ("amount", "quantity"):
        return _decimal_text(value, label)
    if field_name == "inventory_direction":
        return _enum(value, INVENTORY_DIRECTIONS, label, optional=True)
    if field_name == "currency":
        return _optional_text(value, label, max_length=16)
    if field_name == "unit":
        return _optional_text(value, label, max_length=24)
    if field_name == "item_name":
        return _optional_text(value, label, max_length=200)
    if field_name == "description":
        return _optional_text(value, label, max_length=1000)
    raise AIInvalidOutput("%s is not an allowed field" % label)


def validate_correction(payload: Mapping[str, Any]) -> CorrectionResult:
    _require_keys(payload, CORRECTION_KEYS, "correction")
    interpretation = _enum(
        payload["interpretation"], CORRECTION_INTERPRETATIONS, "interpretation", optional=False
    )
    changes_raw = payload["changes"]
    if not isinstance(changes_raw, Mapping):
        raise AIInvalidOutput("changes must be an object")
    unknown = set(changes_raw.keys()) - set(FIELD_NAMES)
    if unknown:
        raise AIInvalidOutput("changes contain unsupported fields: %s" % ",".join(sorted(unknown)))

    changes: Dict[str, Optional[str]] = {}
    for field_name, value in changes_raw.items():
        changes[field_name] = _field_value(field_name, value, prefix="changes.")
    changes = {key: value for key, value in changes.items() if value is not None}
    confidence = _confidence(payload["confidence"])
    reason = _optional_text(payload["reason"], "reason", max_length=500)
    question = _optional_text(payload["clarification_question"], "clarification_question", max_length=400)

    if interpretation == "correction" and not changes:
        raise AIInvalidOutput("a correction must contain at least one changed field")
    if interpretation != "correction" and changes:
        raise AIInvalidOutput("changes must be empty unless interpretation is 'correction'")

    return CorrectionResult(
        interpretation=interpretation,
        changes=changes,
        reason=reason,
        clarification_question=question,
        confidence=confidence,
    )


def validate_report_request(payload: Mapping[str, Any]) -> ReportRequestResult:
    _require_keys(payload, REPORT_REQUEST_KEYS, "report_request")
    is_request = payload["is_report_request"]
    if not isinstance(is_request, bool):
        raise AIInvalidOutput("is_report_request must be a boolean")

    report_type = _enum(payload["report_type"], REPORT_TYPES, "report_type", optional=True)
    period_kind = _enum(payload["period_kind"], PERIOD_KINDS, "period_kind", optional=True)
    start_date = _optional_date(payload["start_date"], "start_date")
    end_date = _optional_date(payload["end_date"], "end_date")
    question = _optional_text(payload["clarification_question"], "clarification_question", max_length=400)

    if not is_request:
        if report_type or period_kind or start_date or end_date:
            raise AIInvalidOutput("non-report output must not describe a report period")
        return ReportRequestResult(
            is_report_request=False,
            report_type=None,
            period_kind=None,
            start_date=None,
            end_date=None,
            clarification_question=None,
            confidence=_confidence(payload["confidence"]),
        )

    if report_type is None:
        report_type = "summary"
    if period_kind is None:
        period_kind = "ambiguous"
    if period_kind == "explicit_date" and not start_date:
        raise AIInvalidOutput("explicit_date requires start_date")
    if period_kind == "range" and not (start_date and end_date):
        raise AIInvalidOutput("range requires start_date and end_date")
    if period_kind in ("today", "yesterday", "last_7_days", "this_week") and (start_date or end_date):
        # The application resolves relative periods deterministically in Tehran.
        start_date = None
        end_date = None

    return ReportRequestResult(
        is_report_request=True,
        report_type=report_type,
        period_kind=period_kind,
        start_date=start_date,
        end_date=end_date,
        clarification_question=question,
        confidence=_confidence(payload["confidence"]),
    )


def validate_narrative(payload: Mapping[str, Any]) -> NarrativeResult:
    _require_keys(payload, NARRATIVE_KEYS, "narrative")
    narrative = payload["narrative"]
    if not isinstance(narrative, str):
        raise AIInvalidOutput("narrative must be a string")
    text = narrative.strip()
    if text.startswith("```") or text.endswith("```"):
        raise AIInvalidOutput("narrative must be plain text, not a code fence")
    text = re.sub(r"[ \t]+", " ", text)
    if not text:
        raise AIInvalidOutput("narrative is empty")
    if len(text) > 1500:
        raise AIInvalidOutput("narrative is longer than 1500 characters")
    return NarrativeResult(narrative=text)

def validate_clarification_answer(payload: Mapping[str, Any]) -> ClarificationAnswerResult:
    _require_keys(payload, CLARIFICATION_KEYS, "clarification_answer")
    values_raw = payload["values"]
    if not isinstance(values_raw, Mapping):
        raise AIInvalidOutput("values must be an object")
    unknown = set(values_raw.keys()) - set(CLARIFICATION_VALUE_KEYS)
    if unknown:
        raise AIInvalidOutput(
            "values contain unsupported fields: %s" % ",".join(sorted(unknown))
        )

    values: Dict[str, Optional[str]] = {}
    for field_name, value in values_raw.items():
        parsed = _field_value(field_name, value, prefix="values.")
        if parsed is not None:
            values[field_name] = parsed

    return ClarificationAnswerResult(
        values=values,
        missing_fields=_string_list(
            payload["missing_fields"], "missing_fields", allowed=FIELD_NAMES
        ),
        ambiguities=_string_list(payload["ambiguities"], "ambiguities"),
        reason=_optional_text(payload["reason"], "reason", max_length=500),
        confidence=_confidence(payload["confidence"]),
    )
