"""Report rendering: deterministic template first, optional AI narrative on top.

The template is always complete and is always sent, so an AI outage can never
lose a report. When narrative generation succeeds it is prepended as a short
summary, but only if a guard proves it introduced no number and no profit claim
that the computed facts do not contain.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from app.context import AppContext
from app.errors import AIError
from app.messages_fa import (
    DIRECTION_LABELS_FA,
    DIRECTION_ORDER,
    empty_totals_note,
    missing_currency_caveat,
    no_events_recorded,
    pending_caveat,
    rejected_caveat,
    report_header,
    report_section,
    undated_pending_caveat,
)
from app.money import format_decimal, to_decimal

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _normalise_digits(text: str) -> str:
    table = {ord(ch): str(index) for index, ch in enumerate(PERSIAN_DIGITS)}
    table.update({ord(ch): str(index) for index, ch in enumerate(ARABIC_DIGITS)})
    return text.translate(table)


def numeric_tokens(text: str) -> set[str]:
    """Digit groups with separators removed, for the narrative safety guard."""

    tokens: set[str] = set()
    for match in _NUMBER_RE.findall(_normalise_digits(text)):
        cleaned = match.replace(",", "").replace("،", "").rstrip(".")
        if cleaned:
            tokens.add(cleaned)
            # Also accept the integer part of a decimal, e.g. "1,500,000.50".
            if "." in cleaned:
                tokens.add(cleaned.split(".")[0])
    return tokens


def facts_numeric_tokens(facts: dict[str, Any]) -> set[str]:
    return numeric_tokens(json.dumps(facts, ensure_ascii=False))


def narrative_is_safe(narrative: str, facts: dict[str, Any]) -> bool:
    if not narrative.strip():
        return False
    # The MVP never computes accounting profit, so the word may not appear.
    if "سود" in narrative:
        return False
    allowed = facts_numeric_tokens(facts)
    return numeric_tokens(narrative).issubset(allowed)


def _money_line(entry: dict[str, Any]) -> str:
    total = format_decimal(to_decimal(str(entry["total"])))
    return f"• {entry['currency']}: {total} ({entry['count']} رکورد)"


def render_template(facts: dict[str, Any], *, title: str, late: bool = False) -> str:
    period = facts["period"]
    blocks = [
        report_header(
            title=title,
            period_start=period["start"],
            period_end=period["end"],
            late=late,
        )
    ]

    counts = facts["counts"]
    if counts["confirmed"] == 0:
        blocks.append(no_events_recorded())

    if facts["sales"]:
        blocks.append(report_section("فروش/درآمد ثبت‌شده:", [_money_line(e) for e in facts["sales"]]))
    if facts["expenses"]:
        blocks.append(
            report_section("هزینه/خرید ثبت‌شده:", [_money_line(e) for e in facts["expenses"]])
        )
    if facts["difference"]:
        lines = [
            f"• {entry['currency']}: {format_decimal(to_decimal(str(entry['amount'])))}"
            for entry in facts["difference"]
        ]
        lines.append("⚠️ این عدد فقط تفاوت مبالغ ثبت‌شده است و سود حسابداری نیست.")
        blocks.append(report_section("اختلاف مبالغ ثبت‌شده:", lines))

    if facts["inventory"]:
        by_direction: dict[str, list[str]] = {}
        for entry in facts["inventory"]:
            label = DIRECTION_LABELS_FA.get(entry["direction"], entry["direction"])
            unit = f" {entry['unit']}" if entry["unit"] else ""
            by_direction.setdefault(label, []).append(
                f"• {entry['item_name'] or 'کالا'}: {format_decimal(to_decimal(str(entry['quantity_total'])))}{unit}"
            )
        lines: list[str] = []
        for direction in DIRECTION_ORDER:
            label = DIRECTION_LABELS_FA[direction]
            if label in by_direction:
                lines.append(f"{label}:")
                lines.extend(by_direction.pop(label))
        for label, extra in by_direction.items():
            lines.append(f"{label}:")
            lines.extend(extra)
        blocks.append(report_section("گردش موجودی:", lines))

    if facts["notes"]:
        blocks.append(
            report_section(
                "رویدادها و یادداشت‌های مهم:",
                [f"• {note['event_date']}: {note['description']}" for note in facts["notes"]],
            )
        )

    caveats: list[str] = []
    if "PENDING_EXCLUDED" in facts["caveats"]:
        caveats.append("• " + pending_caveat(counts["pending"]))
    if "PENDING_WITHOUT_DATE" in facts["caveats"]:
        caveats.append("• " + undated_pending_caveat(facts["counts"]["pending_without_date"]))
    if "REJECTED_EXCLUDED" in facts["caveats"]:
        caveats.append("• " + rejected_caveat(counts["rejected"]))
    if "MISSING_CURRENCY" in facts["caveats"]:
        caveats.append("• " + missing_currency_caveat(counts["missing_currency"]))
    if "NOTES_TRUNCATED" in facts["caveats"]:
        caveats.append(f"• فقط {len(facts['notes'])} یادداشت آخر نمایش داده شده است.")
    if "FOCUSED" in facts["caveats"]:
        caveats.append("• این گزارش فقط بخش درخواستی را نشان می‌دهد.")
    if caveats:
        blocks.append(report_section("داده‌های ناقص یا محدود:", caveats))

    if not any([facts["sales"], facts["expenses"], facts["inventory"], facts["notes"]]) and counts["confirmed"]:
        blocks.append(empty_totals_note())

    blocks.append(f"تعداد رکوردهای تأییدشده: {counts['confirmed']}")
    return "\n\n".join(blocks)


def compose(
    ctx: AppContext,
    facts: dict[str, Any],
    *,
    title: str,
    late: bool = False,
) -> tuple[str, str]:
    """Return ``(text, narrative_source)``. The template is always included."""

    template = render_template(facts, title=title, late=late)
    if not ctx.config.report_narrative_enabled:
        return template, "template"

    try:
        narrative = ctx.ai.narrate_report(facts=facts)
    except AIError:
        return template, "template_ai_unavailable"

    if not narrative_is_safe(narrative, facts):
        ctx.logger.warning(
            "report_narrative_rejected",
            extra={"fields": {"reason": "unsafe_or_unsupported_numbers"}},
        )
        return template, "template_ai_rejected"

    return f"خلاصه: {narrative}\n\n{template}", "ai"


def format_decimal_safe(value: str | Decimal) -> str:
    return format_decimal(to_decimal(str(value)))
