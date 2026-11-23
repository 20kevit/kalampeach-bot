"""Deterministic report facts.

Every figure in a report comes from this module, never from the AI. Amounts are
summed with ``Decimal`` and grouped by the exact currency label supplied by the
employee or the configured default; different currencies are never added
together. Pending and rejected records are excluded from the confirmed totals
and counted separately.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.config import Config
from app.domain import events as domain
from app.money import canonical_decimal, to_decimal
from app.repo import events
from app.timeutil import to_utc_iso

MAX_NOTES_IN_REPORT = 20


def build_facts(
    conn: sqlite3.Connection,
    config: Config,
    *,
    start_date: date,
    end_date: date,
    report_type: str,
    cutoff_utc: datetime | None,
    generated_at: datetime,
    focus: str = "all",
) -> dict[str, Any]:
    rows = events.confirmed_between(
        conn,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        confirmed_before_utc=to_utc_iso(cutoff_utc) if cutoff_utc else None,
    )

    sales: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    expenses: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    inventory: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"quantity": Decimal(0), "count": 0}
    )
    notes: list[dict[str, str]] = []
    missing_currency = 0

    for row in rows:
        event_type = row["event_type"]
        if event_type in domain.FINANCIAL_EVENT_TYPES:
            currency = row["currency"]
            if not currency or not row["amount"]:
                if not currency:
                    missing_currency += 1
                continue
            bucket = sales if event_type == domain.SALE_REVENUE else expenses
            bucket[str(currency)]["total"] += to_decimal(str(row["amount"]))
            bucket[str(currency)]["count"] += 1
        elif event_type == domain.INVENTORY_MOVEMENT:
            key = (
                str(row["inventory_direction"] or "adjustment"),
                str(row["item_name"] or ""),
                str(row["unit"] or ""),
            )
            inventory[key]["quantity"] += to_decimal(str(row["quantity"] or "0"))
            inventory[key]["count"] += 1
        elif event_type == domain.OPERATIONAL_NOTE:
            notes.append(
                {
                    "event_date": str(row["event_date"] or ""),
                    "description": str(row["description"] or ""),
                }
            )

    counts = events.count_status_between(
        conn, start_date=start_date.isoformat(), end_date=end_date.isoformat()
    )
    undated_pending = events.pending_without_date(conn)

    sales_payload = _currency_payload(sales)
    expenses_payload = _currency_payload(expenses)

    difference = []
    for currency in sorted(set(sales) & set(expenses)):
        amount = sales[currency]["total"] - expenses[currency]["total"]
        difference.append(
            {
                "currency": currency,
                "amount": canonical_decimal(amount),
                "label": "difference_of_recorded_amounts",
            }
        )

    inventory_payload = [
        {
            "direction": direction,
            "item_name": item_name,
            "unit": unit,
            "quantity_total": canonical_decimal(values["quantity"]),
            "count": values["count"],
        }
        for (direction, item_name, unit), values in sorted(inventory.items())
    ]

    notes = notes[-MAX_NOTES_IN_REPORT:]

    caveats: list[str] = []
    if counts.get(domain.CONFIRMED, 0) == 0:
        caveats.append("NO_EVENTS")
    if counts.get(domain.PENDING_CLARIFICATION, 0) > 0:
        caveats.append("PENDING_EXCLUDED")
    if undated_pending:
        caveats.append("PENDING_WITHOUT_DATE")
    if counts.get(domain.REJECTED, 0) > 0:
        caveats.append("REJECTED_EXCLUDED")
    if missing_currency:
        caveats.append("MISSING_CURRENCY")
    if len(rows) and len(notes) == MAX_NOTES_IN_REPORT:
        caveats.append("NOTES_TRUNCATED")

    facts: dict[str, Any] = {
        "report_type": report_type,
        "timezone": config.business_timezone,
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "cutoff_utc": to_utc_iso(cutoff_utc) if cutoff_utc else None,
        },
        "generated_at": to_utc_iso(generated_at),
        "focus": focus,
        "counts": {
            "confirmed": counts.get(domain.CONFIRMED, 0),
            "pending": counts.get(domain.PENDING_CLARIFICATION, 0),
            "rejected": counts.get(domain.REJECTED, 0),
            "missing_currency": missing_currency,
            "pending_without_date": undated_pending,
        },
        "sales": sales_payload,
        "expenses": expenses_payload,
        "difference": difference,
        "inventory": inventory_payload,
        "notes": notes,
        "caveats": caveats,
    }

    if focus != "all":
        facts = _apply_focus(facts, focus)

    return facts


def _currency_payload(
    buckets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "currency": currency,
            "total": canonical_decimal(buckets[currency]["total"]),
            "count": buckets[currency]["count"],
        }
        for currency in sorted(buckets)
    ]


def _apply_focus(facts: dict[str, Any], focus: str) -> dict[str, Any]:
    """Manager asked for one category: keep the rest out of the message.

    Counts and caveats stay, so the report still states what was excluded.
    """

    focused = dict(facts)
    keep = {
        "sales": {"sales"},
        "expenses": {"expenses"},
        "inventory": {"inventory"},
        "notes": {"notes"},
    }
    if focus in keep:
        for section in ("sales", "expenses", "inventory", "notes", "difference"):
            if section not in keep[focus]:
                focused[section] = []
        caveats = list(focused.get("caveats", []))
        caveats.append("FOCUSED")
        focused["caveats"] = caveats
    return focused
