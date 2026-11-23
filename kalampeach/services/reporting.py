"""Deterministic report computation and Persian formatting.

The database and this module own every figure. The AI only receives a finished
fact payload and may paraphrase it; :func:`narrative_is_safe` rejects narratives
that mention numbers (or profit language) not present in the computed facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .. import repository
from ..decimal_utils import decimal_to_storage, format_amount, normalize_digits, strip_separators
from ..timeutil import add_days
from . import domain

SALE = "sale_revenue"
EXPENSE = "expense_purchase"
INVENTORY = "inventory_movement"
NOTE = "operational_note"

REPORT_TYPE_TITLES = {
    "summary": "گزارش",
    "full": "گزارش کامل",
    "sales": "گزارش فروش/درآمد",
    "expenses": "گزارش هزینه/خرید",
    "inventory": "گزارش موجودی",
    "incidents": "گزارش رخدادها",
}

NOTE_LIMIT = 10
EMPTY_MESSAGE = "در این بازه هیچ رویدادی ثبت نشده است."
EMPTY_CAVEAT = "نبودِ ثبت در این بازه به معنی نبودِ فعالیت کسب‌وکار نیست."
CURRENCY_NOTE = "مبالغ به تفکیک واحد پول نمایش داده می‌شوند؛ ارزهای مختلف با هم جمع نمی‌شوند."
DIFFERENCE_NOTE = (
    "«اختلاف مبالغ ثبت‌شده» فقط فروش ثبت‌شده منهای هزینهٔ ثبت‌شده است و "
    "سود حسابداری محسوب نمی‌شود."
)
PENDING_NOTE = "موارد در انتظار شفاف‌سازی در جمع‌های تأییدشده لحاظ نشده‌اند."
MISSING_CURRENCY_NOTE = "برخی مبالغ بدون واحد پول مشخص ثبت شده‌اند و در جمع‌ها لحاظ نشده‌اند."
INCOMPLETE_INVENTORY_NOTE = "برخی حرکات موجودی ناقص (کالا/مقدار/واحد) در جمع‌ها لحاظ نشده‌اند."
LATE_NOTE = "این گزارش با تأخیر ارسال می‌شود."

_DIGIT_RUN = re.compile(r"\d+")
_ALLOWED_PROFIT_WORD = "سود"


class PeriodError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()

    @property
    def is_single_day(self) -> bool:
        return self.start == self.end

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1


def resolve_relative_period(kind: str, *, today: date) -> Period:
    """Resolve relative period kinds deterministically in the business timezone."""

    if kind == "today":
        return Period(today, today)
    if kind == "yesterday":
        yesterday = add_days(today, -1)
        return Period(yesterday, yesterday)
    if kind == "last_7_days":
        return Period(add_days(today, -6), today)
    if kind == "this_week":
        # Iranian week starts on Saturday (Python weekday(): Monday=0 .. Saturday=5).
        days_since_saturday = (today.weekday() - 5) % 7
        return Period(add_days(today, -days_since_saturday), today)
    raise PeriodError("unsupported relative period kind")


def resolve_explicit_period(
    *, start: Optional[date], end: Optional[date], today: date, max_days: int
) -> Period:
    if start is None:
        raise PeriodError("start date is required")
    resolved_end = end or start
    if resolved_end < start:
        raise PeriodError("end date is before start date")
    if resolved_end > today:
        raise PeriodError("period ends in the future")
    if (resolved_end - start).days + 1 > max_days:
        raise PeriodError("period is longer than the configured maximum")
    return Period(start, resolved_end)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def compute_facts(
    connection,
    *,
    period: Period,
    timezone_name: str,
    generated_at_utc: str,
) -> Dict[str, object]:
    rows = repository.list_events_for_period(
        connection, start_date=period.start_iso, end_date=period.end_iso
    )
    counts = repository.count_events_by_status_for_period(
        connection, start_date=period.start_iso, end_date=period.end_iso
    )

    monetary: Dict[str, Dict[str, Dict[str, object]]] = {SALE: {}, EXPENSE: {}}
    missing_currency = {SALE: 0, EXPENSE: 0}
    inventory: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    incomplete_inventory = 0
    notes: List[Dict[str, Optional[str]]] = []
    notes_total = 0

    for row in rows:
        if row["status"] != "confirmed":
            continue
        event_type = str(row["event_type"])

        if event_type in (SALE, EXPENSE):
            if row["amount"] is None:
                continue
            amount = Decimal(str(row["amount"]))
            currency = (row["currency"] or "").strip()
            if not currency:
                missing_currency[event_type] += 1
                continue
            key = currency.casefold()
            bucket = monetary[event_type].setdefault(
                key, {"currency": currency, "amount": Decimal("0"), "count": 0}
            )
            bucket["amount"] = bucket["amount"] + amount  # type: ignore[operator]
            bucket["count"] = int(bucket["count"]) + 1
            continue

        if event_type == INVENTORY:
            if not (row["item_name"] and row["quantity"] and row["unit"] and row["inventory_direction"]):
                incomplete_inventory += 1
                continue
            key = (str(row["inventory_direction"]), str(row["item_name"]), str(row["unit"]))
            bucket = inventory.setdefault(
                key, {"direction": key[0], "item": key[1], "unit": key[2], "quantity": Decimal("0"), "count": 0}
            )
            bucket["quantity"] = bucket["quantity"] + Decimal(str(row["quantity"]))  # type: ignore[operator]
            bucket["count"] = int(bucket["count"]) + 1
            continue

        if event_type == NOTE:
            notes_total += 1
            if len(notes) < NOTE_LIMIT:
                notes.append(
                    {
                        "date": str(row["event_date"]),
                        "description": row["description"],
                        "author": _author_label(row),
                    }
                )

    totals: Dict[str, Dict[str, Dict[str, object]]] = {}
    for event_type in (SALE, EXPENSE):
        totals[event_type] = {
            currency_key: {
                "currency": bucket["currency"],
                "amount": decimal_to_storage(bucket["amount"]),
                "count": bucket["count"],
            }
            for currency_key, bucket in sorted(monetary[event_type].items())
        }

    differences: Dict[str, Dict[str, str]] = {}
    for currency_key, sale_bucket in totals[SALE].items():
        expense_bucket = totals[EXPENSE].get(currency_key)
        if not expense_bucket:
            continue
        difference = Decimal(sale_bucket["amount"]) - Decimal(expense_bucket["amount"])
        differences[currency_key] = {
            "currency": sale_bucket["currency"],
            "amount": decimal_to_storage(difference),
        }

    inventory_list = [
        {
            "direction": bucket["direction"],
            "item": bucket["item"],
            "unit": bucket["unit"],
            "quantity": decimal_to_storage(bucket["quantity"]),
            "count": bucket["count"],
        }
        for _, bucket in sorted(inventory.items())
    ]

    facts: Dict[str, object] = {
        "period": {
            "start": period.start_iso,
            "end": period.end_iso,
            "timezone": timezone_name,
        },
        "generated_at_utc": generated_at_utc,
        "totals": totals,
        "differences": differences,
        "inventory": inventory_list,
        "incidents": notes,
        "incidents_total": notes_total,
        "counts": {
            "confirmed": int(counts.get("confirmed", 0)),
            "pending_clarification": int(counts.get("pending_clarification", 0)),
            "rejected": int(counts.get("rejected", 0)),
        },
        "missing_currency": missing_currency,
        "incomplete_inventory": incomplete_inventory,
        "has_confirmed_events": int(counts.get("confirmed", 0)) > 0,
    }
    return facts


def _author_label(row) -> Optional[str]:
    name = row["author_display_name"] if "author_display_name" in row.keys() else None
    return str(name)[:80] if name else None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_report(
    facts: Mapping[str, object],
    *,
    report_type: str = "summary",
    narrative: Optional[str] = None,
    late: bool = False,
    title: Optional[str] = None,
) -> str:
    period = facts["period"]  # type: ignore[index]
    start = str(period["start"])  # type: ignore[index]
    end = str(period["end"])  # type: ignore[index]
    timezone_name = str(period["timezone"])  # type: ignore[index]

    heading = title or REPORT_TYPE_TITLES.get(report_type, REPORT_TYPE_TITLES["summary"])
    span = start if start == end else "%s تا %s" % (start, end)
    lines = ["📊 %s — %s (%s)" % (heading, span, timezone_name), ""]

    counts = facts["counts"]  # type: ignore[index]
    confirmed = int(counts["confirmed"])  # type: ignore[index]
    pending = int(counts["pending_clarification"])  # type: ignore[index]
    rejected = int(counts["rejected"])  # type: ignore[index]

    if not facts["has_confirmed_events"]:
        lines.append(EMPTY_MESSAGE)
        lines.append(EMPTY_CAVEAT)
        if pending:
            lines.append("")
            lines.append("در انتظار شفاف‌سازی: %d مورد" % pending)
            lines.append(PENDING_NOTE)
        if late:
            lines.append("")
            lines.append(LATE_NOTE)
        return "\n".join(lines)

    if narrative:
        lines.append("خلاصه:")
        lines.append(narrative.strip())
    else:
        lines.append("خلاصه:")
        lines.append("در این بازه %d رویداد تأییدشده ثبت شده است." % confirmed)
        if pending:
            lines.append("همچنین %d مورد در انتظار شفاف‌سازی است." % pending)
    lines.append("")

    show_sales = report_type in ("summary", "full", "sales")
    show_expenses = report_type in ("summary", "full", "expenses")
    show_difference = report_type in ("summary", "full")
    show_inventory = report_type in ("summary", "full", "inventory")
    show_incidents = report_type in ("summary", "full", "incidents")

    if show_sales:
        _append_money_section(lines, "فروش/درآمد ثبت‌شده:", facts["totals"][SALE])  # type: ignore[index]
    if show_expenses:
        _append_money_section(lines, "هزینه/خرید ثبت‌شده:", facts["totals"][EXPENSE])  # type: ignore[index]
    if show_difference and facts["differences"]:  # type: ignore[index]
        lines.append("اختلاف مبالغ ثبت‌شده:")
        for bucket in facts["differences"].values():  # type: ignore[index]
            lines.append(
                "- %s: %s" % (bucket["currency"], format_amount(Decimal(bucket["amount"])))
            )
        lines.append(DIFFERENCE_NOTE)
        lines.append("")
    if (show_sales or show_expenses) and facts["missing_currency"]:  # type: ignore[index]
        missing = facts["missing_currency"]  # type: ignore[index]
        total_missing = int(missing[SALE]) + int(missing[EXPENSE])  # type: ignore[index]
        if total_missing:
            lines.append(MISSING_CURRENCY_NOTE)
            lines.append("")
    if show_inventory:
        _append_inventory_section(lines, facts["inventory"])  # type: ignore[index]
        if facts["incomplete_inventory"]:  # type: ignore[index]
            lines.append(INCOMPLETE_INVENTORY_NOTE)
            lines.append("")
    if show_incidents:
        _append_incidents_section(
            lines, facts["incidents"], int(facts["incidents_total"])  # type: ignore[index]
        )

    quality = ["کیفیت داده:", "- تأییدشده: %d" % confirmed]
    if pending:
        quality.append("- در انتظار شفاف‌سازی: %d" % pending)
    if rejected:
        quality.append("- ردشده: %d" % rejected)
    lines.extend(quality)
    if pending:
        lines.append(PENDING_NOTE)
    if (show_sales or show_expenses) and (facts["totals"][SALE] or facts["totals"][EXPENSE]):  # type: ignore[index]
        lines.append(CURRENCY_NOTE)
    if late:
        lines.append("")
        lines.append(LATE_NOTE)
    return "\n".join(lines).strip()


def _append_money_section(lines: List[str], title: str, buckets: Mapping[str, Mapping[str, object]]) -> None:
    lines.append(title)
    if not buckets:
        lines.append("- بدون رکورد")
    else:
        for bucket in buckets.values():
            lines.append(
                "- %s: %s (%d مورد)"
                % (
                    bucket["currency"],
                    format_amount(Decimal(str(bucket["amount"]))),
                    int(bucket["count"]),
                )
            )
    lines.append("")


def _append_inventory_section(lines: List[str], inventory: Sequence[Mapping[str, object]]) -> None:
    lines.append("حرکت موجودی:")
    if not inventory:
        lines.append("- بدون رکورد")
    else:
        for bucket in inventory:
            direction = str(bucket["direction"])
            from .messages import direction_label

            lines.append(
                "- %s: %s — %s %s (%d مورد)"
                % (
                    direction_label(direction),
                    bucket["item"],
                    format_amount(Decimal(str(bucket["quantity"]))),
                    bucket["unit"],
                    int(bucket["count"]),
                )
            )
    lines.append("")


def _append_incidents_section(lines: List[str], incidents, total: int) -> None:
    lines.append("رخدادها و یادداشت‌های مهم:")
    if not incidents:
        lines.append("- بدون رکورد")
    else:
        for item in incidents:
            author = " (ثبت: %s)" % item["author"] if item.get("author") else ""
            lines.append("- %s: %s%s" % (item["date"], item["description"], author))
        if total > len(incidents):
            lines.append("- و %d مورد دیگر" % (total - len(incidents)))
    lines.append("")


# ---------------------------------------------------------------------------
# Narrative guard
# ---------------------------------------------------------------------------


def _numeric_tokens(text: str) -> set:
    normalized = strip_separators(normalize_digits(str(text)))
    return set(_DIGIT_RUN.findall(normalized))


def allowed_numbers(facts: Mapping[str, object]) -> set:
    allowed: set = set()

    def walk(value) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float, Decimal)):
            allowed.update(_numeric_tokens(str(value)))
        elif isinstance(value, str):
            allowed.update(_numeric_tokens(value))

    walk(facts)
    return allowed


def narrative_is_safe(narrative: str, facts: Mapping[str, object]) -> bool:
    """Reject narratives that invent numbers or call a difference profit."""

    if not isinstance(narrative, str) or not narrative.strip():
        return False
    if _ALLOWED_PROFIT_WORD in narrative:
        return False
    return _numeric_tokens(narrative) <= allowed_numbers(facts)
