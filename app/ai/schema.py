"""Strict, application-owned AI output schemas.

Everything here is fail-closed. A model response must be a single JSON object
with exactly the allowed keys; unknown keys, wrong types, invalid enums and
non-decimal money are rejected. Nothing is coerced, defaulted or "repaired" by
the application, because a repaired value would be an invented business fact.

Value-range anomalies (an implausible date, a negative amount) are also
rejected rather than turned into a guess. The source Telegram message is always
preserved, so an operator can see what happened and the employee can restate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from app.domain.events import (
    EVENT_TYPES,
    INVENTORY_DIRECTIONS,
    MAX_BACKDATE_DAYS,
    MAX_FUTURE_DAYS,
)
from app.errors import AIInvalidOutput
from app.money import AmountError, canonical_amount, canonical_quantity, normalise_currency

CLASSIFICATIONS = ("business_event", "non_business", "unclear")

EXTRACTION_KEYS = frozenset(
    {
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
    }
)

# Fields an AI extraction may report as missing; these map to clarification
# questions and are validated against the domain field list.
REPORTABLE_MISSING_FIELDS = frozenset(
    {
        "event_type",
        "event_date",
        "amount",
        "currency",
        "item_name",
        "quantity",
        "unit",
        "inventory_direction",
        "description",
    }
)

CORRECTION_KEYS = frozenset({"patch", "reason"})
INTENT_KEYS = frozenset(
    {
        "intent",
        "period_start",
        "period_end",
        "focus",
        "needs_clarification",
        "question",
    }
)
INTENTS = ("report", "event", "not_business")
FOCUS_VALUES = ("all", "sales", "expenses", "inventory", "notes")

MAX_FREE_TEXT = 2000
MAX_QUESTION = 400


@dataclass(frozen=True)
class CorrectionInterpretation:
    """A proposed change to an existing event. Never applied unvalidated."""

    patch: dict
    reason: str | None


@dataclass(frozen=True)
class ManagerIntent:
    """What a manager's natural-language message asks for."""

    intent: str  # "report" | "event" | "not_business"
    period_start: str | None
    period_end: str | None
    focus: str | None
    needs_clarification: bool
    question: str | None


@dataclass(frozen=True)
class Extraction:
    classification: str
    event_type: str | None
    confidence: float
    event_date: str | None
    amount: str | None
    currency: str | None
    item_name: str | None
    quantity: str | None
    unit: str | None
    inventory_direction: str | None
    description: str | None
    missing_fields: tuple[str, ...]
    ambiguities: tuple[str, ...]
    reason: str | None


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AIInvalidOutput(f"{what} must be a JSON object")
    return value


def _require_exact_keys(payload: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    extra = set(payload) - allowed
    missing = allowed - set(payload)
    if extra:
        raise AIInvalidOutput(f"{what} contains unsupported keys")
    if missing:
        raise AIInvalidOutput(f"{what} is missing required keys")


def _optional_text(value: Any, what: str, *, max_length: int = MAX_FREE_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIInvalidOutput(f"{what} must be text or null")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > max_length:
        raise AIInvalidOutput(f"{what} is longer than {max_length} characters")
    return text


def _optional_str_list(value: Any, what: str, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AIInvalidOutput(f"{what} must be a list of strings")
    items = tuple(item.strip() for item in value if item.strip())
    if allowed is not None:
        for item in items:
            if item not in allowed:
                raise AIInvalidOutput(f"{what} contains an unsupported field name")
    return items


def _parse_iso_date(value: Any, what: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIInvalidOutput(f"{what} must be an ISO date string or null")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise AIInvalidOutput(f"{what} must use YYYY-MM-DD") from exc


def _check_date_window(parsed: date | None, message_local_date: date | None, what: str) -> None:
    if parsed is None or message_local_date is None:
        return
    if parsed < message_local_date - timedelta(days=MAX_BACKDATE_DAYS):
        raise AIInvalidOutput(f"{what} is implausibly old for the source message")
    if parsed > message_local_date + timedelta(days=MAX_FUTURE_DAYS):
        raise AIInvalidOutput(f"{what} is in the future")


def validate_extraction(
    payload: Any, *, message_local_date: date | None = None
) -> Extraction:
    data = _require_mapping(payload, "extraction")
    _require_exact_keys(data, EXTRACTION_KEYS, "extraction")

    classification = data["classification"]
    if classification not in CLASSIFICATIONS:
        raise AIInvalidOutput("classification is not an allowed value")

    event_type = data["event_type"]
    if event_type is not None:
        if event_type not in EVENT_TYPES:
            raise AIInvalidOutput("event_type is not an allowed value")

    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise AIInvalidOutput("confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise AIInvalidOutput("confidence must be between 0 and 1")

    parsed_date = _parse_iso_date(data["event_date"], "event_date")
    _check_date_window(parsed_date, message_local_date, "event_date")

    amount: str | None = None
    if data["amount"] is not None:
        if isinstance(data["amount"], float):
            raise AIInvalidOutput("amount must be a decimal string, not a float")
        try:
            amount = canonical_amount(data["amount"], "amount")
        except AmountError as exc:
            raise AIInvalidOutput("amount is not a positive decimal value") from exc

    quantity: str | None = None
    if data["quantity"] is not None:
        if isinstance(data["quantity"], float):
            raise AIInvalidOutput("quantity must be a decimal string, not a float")
        try:
            quantity = canonical_quantity(data["quantity"], "quantity")
        except AmountError as exc:
            raise AIInvalidOutput("quantity is not a positive decimal value") from exc

    try:
        currency = normalise_currency(data["currency"])
    except AmountError as exc:
        raise AIInvalidOutput("currency is not a valid label") from exc

    direction = data["inventory_direction"]
    if direction is not None and direction not in INVENTORY_DIRECTIONS:
        raise AIInvalidOutput("inventory_direction is not an allowed value")

    item_name = _optional_text(data["item_name"], "item_name", max_length=200)
    unit = _optional_text(data["unit"], "unit", max_length=32)
    description = _optional_text(data["description"], "description")
    reason = _optional_text(data["reason"], "reason", max_length=500)

    missing_fields = _optional_str_list(
        data["missing_fields"], "missing_fields", allowed=REPORTABLE_MISSING_FIELDS
    )
    ambiguities = _optional_str_list(data["ambiguities"], "ambiguities")

    if classification == "business_event" and event_type is None:
        # Business but the category is unclear: the application asks, it never
        # picks a category on the model's behalf.
        missing_fields = tuple(sorted(set(missing_fields) | {"event_type"}))

    if classification != "business_event" and (
        event_type is not None or amount is not None or quantity is not None
    ):
        # A non-business classification must not carry business payload that a
        # later stage might accidentally store.
        raise AIInvalidOutput("non-business output must not contain event values")


    return Extraction(
        classification=str(classification),
        event_type=event_type,
        confidence=float(confidence),
        event_date=parsed_date.isoformat() if parsed_date else None,
        amount=amount,
        currency=currency,
        item_name=item_name,
        quantity=quantity,
        unit=unit,
        inventory_direction=direction,
        description=description,
        missing_fields=missing_fields,
        ambiguities=ambiguities,
        reason=reason,
    )


def validate_correction(
    payload: Any, *, message_local_date: date | None = None
) -> CorrectionInterpretation:
    from app.domain.events import MUTABLE_FIELDS

    data = _require_mapping(payload, "correction")
    _require_exact_keys(data, CORRECTION_KEYS, "correction")
    patch_raw = data["patch"]
    if not isinstance(patch_raw, Mapping):
        raise AIInvalidOutput("correction patch must be a JSON object")
    unknown = set(patch_raw) - set(MUTABLE_FIELDS)
    if unknown:
        raise AIInvalidOutput("correction patch contains unsupported fields")

    patch: dict[str, Any] = {}
    for key, value in patch_raw.items():
        if key == "event_type":
            if value not in EVENT_TYPES:
                raise AIInvalidOutput("correction event_type is not allowed")
            patch[key] = value
        elif key == "event_date":
            parsed = _parse_iso_date(value, "correction event_date")
            _check_date_window(parsed, message_local_date, "correction event_date")
            patch[key] = parsed.isoformat() if parsed else None
        elif key == "amount":
            if value is None:
                patch[key] = None
                continue
            if isinstance(value, float):
                raise AIInvalidOutput("correction amount must not be a float")
            try:
                patch[key] = canonical_amount(value, "amount")
            except AmountError as exc:
                raise AIInvalidOutput("correction amount is not a positive decimal") from exc
        elif key == "quantity":
            if value is None:
                patch[key] = None
                continue
            if isinstance(value, float):
                raise AIInvalidOutput("correction quantity must not be a float")
            try:
                patch[key] = canonical_quantity(value, "quantity")
            except AmountError as exc:
                raise AIInvalidOutput("correction quantity is not a positive decimal") from exc
        elif key == "currency":
            try:
                patch[key] = normalise_currency(value)
            except AmountError as exc:
                raise AIInvalidOutput("correction currency is not a valid label") from exc
        elif key == "inventory_direction":
            if value is not None and value not in INVENTORY_DIRECTIONS:
                raise AIInvalidOutput("correction inventory_direction is not allowed")
            patch[key] = value
        elif key == "item_name":
            patch[key] = _optional_text(value, "item_name", max_length=200)
        elif key == "unit":
            patch[key] = _optional_text(value, "unit", max_length=32)
        elif key == "description":
            patch[key] = _optional_text(value, "description")
        elif key == "notes":
            patch[key] = _optional_text(value, "notes")
        else:  # pragma: no cover - guarded by the unknown-key check
            raise AIInvalidOutput("correction patch contains unsupported fields")

    reason = _optional_text(data["reason"], "reason", max_length=500)
    return CorrectionInterpretation(patch=patch, reason=reason)


def validate_manager_intent(payload: Any, *, today: date, max_range_days: int) -> ManagerIntent:
    data = _require_mapping(payload, "manager intent")
    _require_exact_keys(data, INTENT_KEYS, "manager intent")

    intent = data["intent"]
    if intent not in INTENTS:
        raise AIInvalidOutput("intent is not an allowed value")

    needs_clarification = data["needs_clarification"]
    if not isinstance(needs_clarification, bool):
        raise AIInvalidOutput("needs_clarification must be a boolean")

    question = _optional_text(data["question"], "question", max_length=MAX_QUESTION)

    focus = data["focus"]
    if focus is not None and focus not in FOCUS_VALUES:
        raise AIInvalidOutput("focus is not an allowed value")

    start = _parse_iso_date(data["period_start"], "period_start")
    end = _parse_iso_date(data["period_end"], "period_end")

    if intent == "report" and not needs_clarification:
        if start is None or end is None:
            raise AIInvalidOutput("a report intent requires a complete period")
        if end < start:
            raise AIInvalidOutput("period_end must not precede period_start")
        if end > today:
            raise AIInvalidOutput("period_end must not be in the future")
        if (end - start).days + 1 > max_range_days:
            raise AIInvalidOutput("requested report range is too wide")

    return ManagerIntent(
        intent=str(intent),
        period_start=start.isoformat() if start else None,
        period_end=end.isoformat() if end else None,
        focus=focus,
        needs_clarification=bool(needs_clarification),
        question=question,
    )


def validate_narrative(text: Any, *, max_length: int = 1500) -> str:
    if not isinstance(text, str):
        raise AIInvalidOutput("narrative must be text")
    cleaned = text.strip()
    if not cleaned:
        raise AIInvalidOutput("narrative is empty")
    if len(cleaned) > max_length:
        raise AIInvalidOutput("narrative is too long")
    return cleaned
