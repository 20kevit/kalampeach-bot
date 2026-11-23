"""Deterministic event semantics: required fields, dates, money, snapshots.

The AI proposes values; this module decides whether they are usable. Nothing in
here talks to the database, Telegram, or the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..ai.types import ExtractionResult
from ..decimal_utils import DecimalValueError, decimal_to_storage, parse_decimal
from ..timeutil import TimeError, parse_business_date

FINANCIAL_TYPES = ("sale_revenue", "expense_purchase")
EVENT_TYPES = ("sale_revenue", "expense_purchase", "inventory_movement", "operational_note")
INVENTORY_DIRECTIONS = ("received", "consumed", "wasted", "adjustment")

FIELD_ORDER = (
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

REQUIRED_FIELDS = {
    "sale_revenue": ("event_date", "amount", "currency"),
    "expense_purchase": ("event_date", "amount", "currency"),
    "inventory_movement": ("event_date", "item_name", "quantity", "unit", "inventory_direction"),
    "operational_note": ("event_date", "description"),
}

# How far back an explicit date may point before the application asks instead of
# trusting the model.
NEW_EVENT_MAX_BACK_DAYS = 31
CORRECTION_MAX_BACK_DAYS = 366

MAX_AMOUNT = Decimal("1000000000000000")
MAX_QUANTITY = Decimal("1000000000")

TEXT_LIMITS = {
    "currency": 16,
    "item_name": 200,
    "unit": 24,
    "description": 1000,
}


class DomainError(ValueError):
    """Raised when a draft cannot be interpreted deterministically."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class EventDraft:
    event_type: str
    event_date: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    currency_source: Optional[str] = None
    item_name: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    inventory_direction: Optional[str] = None
    description: Optional[str] = None

    def storage(self) -> Dict[str, Optional[str]]:
        """Column values (decimals serialized as plain strings)."""

        return {
            "event_type": self.event_type,
            "event_date": self.event_date,
            "amount": decimal_to_storage(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "currency_source": self.currency_source,
            "item_name": self.item_name,
            "quantity": decimal_to_storage(self.quantity) if self.quantity is not None else None,
            "unit": self.unit,
            "inventory_direction": self.inventory_direction,
            "description": self.description,
        }

    def snapshot(self, *, status: str) -> Dict[str, Optional[str]]:
        data = self.storage()
        data["status"] = status
        return data


@dataclass(frozen=True)
class DraftAssessment:
    draft: EventDraft
    missing_fields: Tuple[str, ...]
    notes: Tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields


def _clean_text(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    limit = TEXT_LIMITS.get(field, 1000)
    return text[:limit]


def _to_decimal(value: Optional[str], field: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        parsed = parse_decimal(value, field=field)
    except DecimalValueError:
        return None
    return parsed


def resolve_business_date(
    candidate: Optional[str], message_local_date: str, *, max_back_days: int
) -> Tuple[str, bool]:
    """Return ``(date_to_store, had_to_fall_back)``.

    A missing date deterministically uses the source message's Tehran date.
    A date outside a plausible window is *not* trusted: the message date is used
    for storage and the field is reported as needing clarification.
    """

    try:
        message_date = parse_business_date(message_local_date)
    except TimeError as exc:
        raise DomainError("invalid message date") from exc

    if candidate is None:
        return message_date.isoformat(), False
    try:
        parsed = parse_business_date(candidate)
    except TimeError:
        return message_date.isoformat(), True

    latest = message_date + timedelta(days=1)
    earliest = message_date - timedelta(days=max_back_days)
    if parsed > latest or parsed < earliest:
        return message_date.isoformat(), True
    return parsed.isoformat(), False


def missing_fields_for(draft: EventDraft) -> Tuple[str, ...]:
    """Deterministic required-field check for the draft's event type."""

    missing: List[str] = []
    if draft.event_type not in EVENT_TYPES:
        return ("event_type",)
    try:
        parse_business_date(draft.event_date)
    except TimeError:
        missing.append("event_date")

    if draft.event_type in FINANCIAL_TYPES:
        if draft.amount is None or draft.amount <= 0 or draft.amount > MAX_AMOUNT:
            missing.append("amount")
        if not draft.currency:
            missing.append("currency")
    elif draft.event_type == "inventory_movement":
        if not draft.item_name:
            missing.append("item_name")
        if draft.quantity is None or draft.quantity <= 0 or draft.quantity > MAX_QUANTITY:
            missing.append("quantity")
        if not draft.unit:
            missing.append("unit")
        if draft.inventory_direction not in INVENTORY_DIRECTIONS:
            missing.append("inventory_direction")
    elif draft.event_type == "operational_note":
        if not draft.description or len(draft.description) < 3:
            missing.append("description")

    if not draft.event_date:
        missing.append("event_date")
    return tuple(field for field in FIELD_ORDER if field in set(missing))


def assess_new_event(
    extraction: ExtractionResult,
    *,
    message_local_date: str,
    default_currency: Optional[str],
) -> DraftAssessment:
    """Turn a validated extraction into a draft plus its missing-field list."""

    if extraction.classification != "business_event" or extraction.event_type not in EVENT_TYPES:
        raise DomainError("extraction is not a supported business event")

    notes: List[str] = []
    event_date, date_fallback = resolve_business_date(
        extraction.event_date, message_local_date, max_back_days=NEW_EVENT_MAX_BACK_DAYS
    )
    if date_fallback:
        notes.append("event_date_not_trusted")

    currency = _clean_text(extraction.currency, "currency")
    currency_source: Optional[str] = None
    if currency:
        currency_source = "explicit"
    elif default_currency:
        currency = default_currency
        currency_source = "default"
        notes.append("currency_default_applied")

    amount = _to_decimal(extraction.amount, "amount") if extraction.amount else None
    quantity = _to_decimal(extraction.quantity, "quantity") if extraction.quantity else None

    draft = EventDraft(
        event_type=extraction.event_type,
        event_date=event_date,
        amount=amount,
        currency=currency,
        currency_source=currency_source if extraction.event_type in FINANCIAL_TYPES else None,
        item_name=_clean_text(extraction.item_name, "item_name")
        if extraction.event_type == "inventory_movement"
        else None,
        quantity=quantity if extraction.event_type == "inventory_movement" else None,
        unit=_clean_text(extraction.unit, "unit") if extraction.event_type == "inventory_movement" else None,
        inventory_direction=extraction.inventory_direction
        if extraction.event_type == "inventory_movement"
        else None,
        description=_clean_text(extraction.description, "description"),
    )

    missing = missing_fields_for(draft)
    if date_fallback and "event_date" not in missing:
        missing = tuple(sorted(set(missing) | {"event_date"}, key=FIELD_ORDER.index))
    return DraftAssessment(draft=draft, missing_fields=missing, notes=tuple(notes))


def assess_correction(
    current: EventDraft,
    changes: Mapping[str, Optional[str]],
    *,
    message_local_date: str,
) -> DraftAssessment:
    """Apply a validated patch to the current draft and re-validate semantics."""

    if not changes:
        raise DomainError("empty correction")
    notes: List[str] = []
    draft = current

    if "event_type" in changes:
        new_type = changes["event_type"]
        if new_type not in EVENT_TYPES:
            raise DomainError("unsupported event_type in correction")
        draft = replace(draft, event_type=new_type)

    if "event_date" in changes:
        resolved, fallback = resolve_business_date(
            changes["event_date"], message_local_date, max_back_days=CORRECTION_MAX_BACK_DAYS
        )
        if fallback:
            notes.append("event_date_not_trusted")
        draft = replace(draft, event_date=resolved)

    if "amount" in changes:
        parsed = _to_decimal(changes["amount"], "amount")
        if parsed is None:
            raise DomainError("amount in correction is not a decimal")
        if parsed <= 0 or parsed > MAX_AMOUNT:
            raise DomainError("amount in correction is out of range")
        draft = replace(draft, amount=parsed, currency_source="explicit")

    if "currency" in changes:
        currency = _clean_text(changes["currency"], "currency")
        if not currency:
            raise DomainError("currency in correction is empty")
        draft = replace(draft, currency=currency, currency_source="explicit")

    if "quantity" in changes:
        parsed = _to_decimal(changes["quantity"], "quantity")
        if parsed is None or parsed <= 0 or parsed > MAX_QUANTITY:
            raise DomainError("quantity in correction is out of range")
        draft = replace(draft, quantity=parsed)

    if "item_name" in changes:
        draft = replace(draft, item_name=_clean_text(changes["item_name"], "item_name"))
    if "unit" in changes:
        draft = replace(draft, unit=_clean_text(changes["unit"], "unit"))
    if "description" in changes:
        draft = replace(draft, description=_clean_text(changes["description"], "description"))
    if "inventory_direction" in changes:
        direction = changes["inventory_direction"]
        if direction not in INVENTORY_DIRECTIONS:
            raise DomainError("unsupported inventory direction")
        draft = replace(draft, inventory_direction=direction)

    # Keep the projection self-consistent: fields that only apply to one type
    # are cleared when the type changes.
    if "event_type" in changes:
        if draft.event_type == "operational_note":
            draft = replace(
                draft,
                amount=None,
                currency=None,
                currency_source=None,
                item_name=None,
                quantity=None,
                unit=None,
                inventory_direction=None,
            )
        elif draft.event_type in FINANCIAL_TYPES:
            draft = replace(draft, item_name=None, quantity=None, unit=None, inventory_direction=None)
        elif draft.event_type == "inventory_movement":
            draft = replace(draft, amount=None, currency=None, currency_source=None)

    missing = missing_fields_for(draft)
    if "event_date" in notes and "event_date" not in missing:
        missing = tuple(sorted(set(missing) | {"event_date"}, key=FIELD_ORDER.index))
    return DraftAssessment(draft=draft, missing_fields=missing, notes=tuple(notes))


def merge_clarification_answer(
    current: EventDraft,
    answer: ExtractionResult,
    missing_fields: Sequence[str],
    *,
    message_local_date: str,
) -> DraftAssessment:
    """Fill only the missing fields from a clarification answer.

    Values already known are never overwritten by the answer (documented in
    DECISIONS.md), so an ambiguous clarification cannot silently change a
    previously recorded amount.
    """

    changes: Dict[str, Optional[str]] = {}
    wanted = set(missing_fields)
    candidate = assess_new_event(
        ExtractionResult(
            classification="business_event",
            event_type=current.event_type,
            confidence=answer.confidence,
            event_date=answer.event_date,
            amount=answer.amount,
            currency=answer.currency,
            item_name=answer.item_name,
            quantity=answer.quantity,
            unit=answer.unit,
            inventory_direction=answer.inventory_direction,
            description=answer.description,
            missing_fields=answer.missing_fields,
            ambiguities=answer.ambiguities,
            reason=answer.reason,
        ),
        message_local_date=message_local_date,
        default_currency=None,
    ).draft

    if "event_date" in wanted:
        changes["event_date"] = candidate.event_date
    if "amount" in wanted and candidate.amount is not None:
        changes["amount"] = decimal_to_storage(candidate.amount)
    if "currency" in wanted and candidate.currency:
        changes["currency"] = candidate.currency
    if "item_name" in wanted and candidate.item_name:
        changes["item_name"] = candidate.item_name
    if "quantity" in wanted and candidate.quantity is not None:
        changes["quantity"] = decimal_to_storage(candidate.quantity)
    if "unit" in wanted and candidate.unit:
        changes["unit"] = candidate.unit
    if "inventory_direction" in wanted and candidate.inventory_direction:
        changes["inventory_direction"] = candidate.inventory_direction
    if "description" in wanted and candidate.description:
        changes["description"] = candidate.description

    draft = current
    if "amount" in changes:
        draft = replace(draft, amount=parse_decimal(changes["amount"], field="amount"))
    if "currency" in changes:
        draft = replace(
            draft, currency=changes["currency"], currency_source="explicit"
        )
    if "event_date" in changes:
        draft = replace(draft, event_date=changes["event_date"])
    if "item_name" in changes:
        draft = replace(draft, item_name=changes["item_name"])
    if "quantity" in changes:
        draft = replace(draft, quantity=parse_decimal(changes["quantity"], field="quantity"))
    if "unit" in changes:
        draft = replace(draft, unit=changes["unit"])
    if "inventory_direction" in changes:
        draft = replace(draft, inventory_direction=changes["inventory_direction"])
    if "description" in changes:
        draft = replace(draft, description=changes["description"])

    missing = missing_fields_for(draft)
    return DraftAssessment(draft=draft, missing_fields=missing, notes=tuple())


def draft_from_row(row: Mapping[str, object]) -> EventDraft:
    """Rebuild a draft from an ``events`` row."""

    def _decimal(name: str) -> Optional[Decimal]:
        value = row[name]
        if value is None:
            return None
        return parse_decimal(str(value), field=name)

    return EventDraft(
        event_type=str(row["event_type"]),
        event_date=str(row["event_date"]),
        amount=_decimal("amount"),
        currency=row["currency"] if row["currency"] is not None else None,
        currency_source=row["currency_source"] if row["currency_source"] is not None else None,
        item_name=row["item_name"] if row["item_name"] is not None else None,
        quantity=_decimal("quantity"),
        unit=row["unit"] if row["unit"] is not None else None,
        inventory_direction=row["inventory_direction"] if row["inventory_direction"] is not None else None,
        description=row["description"] if row["description"] is not None else None,
    )


def snapshot_json(snapshot: Mapping[str, Optional[str]]) -> str:
    return json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True)


def ordered_fields(fields: Iterable[str]) -> Tuple[str, ...]:
    return tuple(field for field in FIELD_ORDER if field in set(fields))
