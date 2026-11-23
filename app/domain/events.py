"""Domain model for operational events.

This module owns the deterministic rules: which fields exist, which fields are
required per event type, how AI extraction is normalised into storable fields,
and how snapshots are built for the append-only revision history.

The AI never decides any of this; it only proposes values that are validated
here. Nullable columns mean "unknown or not applicable" and are never coerced
to zero.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Mapping

from app.errors import DomainValidationError
from app.money import AmountError, canonical_amount, canonical_quantity, normalise_currency, parse_decimal

SALE_REVENUE = "sale_revenue"
EXPENSE_PURCHASE = "expense_purchase"
INVENTORY_MOVEMENT = "inventory_movement"
OPERATIONAL_NOTE = "operational_note"

EVENT_TYPES = (SALE_REVENUE, EXPENSE_PURCHASE, INVENTORY_MOVEMENT, OPERATIONAL_NOTE)
FINANCIAL_EVENT_TYPES = (SALE_REVENUE, EXPENSE_PURCHASE)

CONFIRMED = "confirmed"
PENDING_CLARIFICATION = "pending_clarification"
REJECTED = "rejected"
EVENT_STATUSES = (CONFIRMED, PENDING_CLARIFICATION, REJECTED)

RECEIVED = "received"
CONSUMED = "consumed"
WASTED = "wasted"
ADJUSTMENT = "adjustment"
INVENTORY_DIRECTIONS = (RECEIVED, CONSUMED, WASTED, ADJUSTMENT)

# Fields the AI may propose and a correction may change.
MUTABLE_FIELDS = (
    "event_type",
    "event_date",
    "amount",
    "currency",
    "item_name",
    "quantity",
    "unit",
    "inventory_direction",
    "description",
    "notes",
)

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    SALE_REVENUE: ("event_date", "amount", "currency"),
    EXPENSE_PURCHASE: ("event_date", "amount", "currency"),
    INVENTORY_MOVEMENT: ("event_date", "item_name", "quantity", "unit", "inventory_direction"),
    OPERATIONAL_NOTE: ("event_date", "description"),
}

FIELD_LABELS_FA = {
    "event_date": "تاریخ رویداد",
    "amount": "مبلغ",
    "currency": "واحد پول",
    "item_name": "نام کالا",
    "quantity": "مقدار",
    "unit": "واحد اندازه‌گیری",
    "inventory_direction": "نوع گردش موجودی (ورود/مصرف/ضایعات/اصلاح)",
    "description": "توضیح رویداد",
    "event_type": "نوع رویداد",
}

MAX_TEXT_LENGTH = 1000
MAX_NOTES_LENGTH = 2000
MAX_ITEM_NAME_LENGTH = 200
MAX_UNIT_LENGTH = 32

# Guard against hallucinated dates: a business event may be backdated at most
# this many days and at most one day into the future. Anything else is treated
# as an ambiguity and clarified with the employee.
MAX_BACKDATE_DAYS = 31
MAX_FUTURE_DAYS = 1

EVENT_TYPE_LABELS_FA = {
    SALE_REVENUE: "فروش/درآمد",
    EXPENSE_PURCHASE: "هزینه/خرید",
    INVENTORY_MOVEMENT: "گردش موجودی",
    OPERATIONAL_NOTE: "یادداشت عملیاتی",
}

DIRECTION_LABELS_FA = {
    RECEIVED: "ورود",
    CONSUMED: "مصرف",
    WASTED: "ضایعات",
    ADJUSTMENT: "اصلاح موجودی",
}


def empty_fields() -> dict[str, Any]:
    return {field: None for field in MUTABLE_FIELDS}


def validate_event_type(value: object) -> str:
    if value not in EVENT_TYPES:
        raise DomainValidationError(["event_type is not a supported event type"])
    return str(value)


def validate_event_date(value: object, *, message_local_date: date | None) -> str | None:
    """Validate an ISO business date against the plausibility window."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(["event_date must be an ISO date string"])
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DomainValidationError(["event_date must use YYYY-MM-DD"]) from exc
    if message_local_date is not None:
        earliest = message_local_date - timedelta(days=MAX_BACKDATE_DAYS)
        latest = message_local_date + timedelta(days=MAX_FUTURE_DAYS)
        if parsed < earliest or parsed > latest:
            raise DomainValidationError(
                ["event_date is outside the plausible range for the message"]
            )
    return parsed.isoformat()


def _clean_text(value: object, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError([f"{field} must be text"])
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def normalise_fields(
    raw: Mapping[str, Any],
    *,
    message_local_date: date | None,
    default_currency: str | None = None,
    allow_message_date_as_event_date: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalise a proposed field mapping into storable canonical values.

    Returns ``(fields, details)``. ``details`` records audit-worthy decisions
    (for example that a default currency was applied) without inventing data.

    Raises :class:`DomainValidationError` when a supplied value is malformed.
    Missing required values are *not* an error here: they become clarifications.
    """

    problems: list[str] = []
    fields = empty_fields()
    details: dict[str, Any] = {}

    event_type = raw.get("event_type")
    if event_type is not None:
        if event_type in EVENT_TYPES:
            fields["event_type"] = event_type
        else:
            problems.append("event_type is not a supported event type")

    try:
        fields["event_date"] = validate_event_date(
            raw.get("event_date"), message_local_date=message_local_date
        )
    except DomainValidationError as exc:
        problems.extend(exc.problems)

    try:
        amount_value = raw.get("amount")
        if amount_value is not None:
            fields["amount"] = canonical_amount(amount_value, "amount")
    except AmountError as exc:
        problems.append(str(exc))

    try:
        fields["currency"] = normalise_currency(raw.get("currency"))
    except AmountError as exc:
        problems.append(str(exc))

    try:
        fields["item_name"] = _clean_text(
            raw.get("item_name"), field="item_name", max_length=MAX_ITEM_NAME_LENGTH
        )
        fields["description"] = _clean_text(
            raw.get("description"), field="description", max_length=MAX_TEXT_LENGTH
        )
        fields["notes"] = _clean_text(
            raw.get("notes"), field="notes", max_length=MAX_NOTES_LENGTH
        )
        fields["unit"] = _clean_text(raw.get("unit"), field="unit", max_length=MAX_UNIT_LENGTH)
    except DomainValidationError as exc:
        problems.extend(exc.problems)

    try:
        quantity_value = raw.get("quantity")
        if quantity_value is not None:
            fields["quantity"] = canonical_quantity(quantity_value, "quantity")
    except AmountError as exc:
        problems.append(str(exc))

    direction = raw.get("inventory_direction")
    if direction is not None:
        if direction in INVENTORY_DIRECTIONS:
            fields["inventory_direction"] = direction
        else:
            problems.append("inventory_direction is not a supported value")

    if problems:
        raise DomainValidationError(problems)

    # Currency policy: only an owner-configured default may fill a gap, and the
    # fact is recorded in details for auditability.
    effective_type = fields["event_type"]
    if (
        effective_type in FINANCIAL_EVENT_TYPES
        and fields["currency"] is None
        and default_currency
    ):
        fields["currency"] = normalise_currency(default_currency)
        details["default_currency_applied"] = True

    # Date policy: never infer a date unless the owner explicitly allowed it.
    if fields["event_date"] is None and message_local_date is not None:
        if allow_message_date_as_event_date:
            fields["event_date"] = message_local_date.isoformat()
            details["event_date_from_message"] = True
        elif effective_type is not None:
            details.setdefault("needs_date_decision", True)

    _drop_inapplicable_fields(fields, details)
    return fields, details


def _drop_inapplicable_fields(fields: dict[str, Any], details: dict[str, Any]) -> None:
    """Keep the payload semantically consistent with the event type.

    Values that do not apply to the chosen category are removed rather than
    reinterpreted, and the removal is recorded so an audit reader can see it.
    """

    event_type = fields["event_type"]
    dropped: list[str] = []

    if event_type is None:
        # Category not known yet (pending clarification): keep whatever the
        # employee stated so the answer can complete the same candidate.
        return

    if event_type not in FINANCIAL_EVENT_TYPES:
        for field in ("amount", "currency"):
            if fields[field] is not None:
                fields[field] = None
                dropped.append(field)

    if event_type != INVENTORY_MOVEMENT:
        for field in ("inventory_direction", "item_name", "quantity", "unit"):
            if fields[field] is not None:
                fields[field] = None
                dropped.append(field)

    if event_type == OPERATIONAL_NOTE and fields.get("notes") is not None:
        # A note's description is the note; keep notes only as extra detail.
        pass

    if dropped:
        details["dropped_inapplicable_fields"] = sorted(set(dropped))


def missing_required_fields(fields: Mapping[str, Any]) -> list[str]:
    event_type = fields.get("event_type")
    if event_type not in EVENT_TYPES:
        return ["event_type"]
    return [field for field in REQUIRED_FIELDS[event_type] if fields.get(field) is None]


def validate_semantics(fields: Mapping[str, Any]) -> None:
    """Validate cross-field consistency. Raises :class:`DomainValidationError`."""

    problems: list[str] = []
    event_type = fields.get("event_type")
    if event_type not in EVENT_TYPES:
        problems.append("event_type is required")
        raise DomainValidationError(problems)

    if event_type in FINANCIAL_EVENT_TYPES:
        if fields.get("amount") is None:
            problems.append("amount is required for a financial event")
        else:
            try:
                parse_decimal(fields["amount"], "amount")
            except AmountError as exc:
                problems.append(str(exc))
        if fields.get("currency") is None:
            problems.append("currency is required for a financial event")
        if fields.get("inventory_direction") is not None:
            problems.append("inventory_direction does not apply to a financial event")

    if event_type == INVENTORY_MOVEMENT:
        for field in ("item_name", "quantity", "unit", "inventory_direction"):
            if fields.get(field) is None:
                problems.append(f"{field} is required for an inventory movement")
        if fields.get("quantity") is not None:
            try:
                parse_decimal(fields["quantity"], "quantity")
            except AmountError as exc:
                problems.append(str(exc))
        if fields.get("amount") is not None:
            problems.append("amount does not apply to an inventory movement")

    if event_type == OPERATIONAL_NOTE and fields.get("description") is None:
        problems.append("description is required for an operational note")

    if not fields.get("event_date"):
        problems.append("event_date is required")
    else:
        try:
            date.fromisoformat(str(fields["event_date"]))
        except ValueError:
            problems.append("event_date must use YYYY-MM-DD")

    if problems:
        raise DomainValidationError(problems)


def snapshot(fields: Mapping[str, Any], *, status: str | None = None) -> dict[str, Any]:
    """Structured snapshot stored in revision history."""

    data = {field: fields.get(field) for field in MUTABLE_FIELDS}
    if status is not None:
        data["status"] = status
    return data


def snapshot_json(fields: Mapping[str, Any], *, status: str | None = None) -> str:
    return json.dumps(snapshot(fields, status=status), ensure_ascii=False, sort_keys=True)


def fields_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in MUTABLE_FIELDS}


def apply_patch(
    current: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a correction patch into the current snapshot."""

    merged = {field: current.get(field) for field in MUTABLE_FIELDS}
    for key, value in patch.items():
        if key not in MUTABLE_FIELDS:
            raise DomainValidationError([f"{key} is not a correctable field"])
        merged[key] = value
    return merged
