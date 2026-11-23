"""Persian user-facing text.

Keeping every employee/manager string in one module makes the tone easy to
review and guarantees that no internal identifier, prompt, raw JSON or stack
trace can leak into a chat by accident.

Rules reflected here:
* clarify privately, never in the group;
* never print a total we did not compute deterministically;
* never call the difference of recorded amounts "profit";
* state explicitly when a period has no records instead of implying zero sales.
"""

from __future__ import annotations

from app.domain.events import (
    CONSUMED,
    DIRECTION_LABELS_FA,
    EVENT_TYPE_LABELS_FA,
    RECEIVED,
    WASTED,
    ADJUSTMENT,
)
from app.money import format_decimal, to_decimal

WELCOME_EMPLOYEE = (
    "سلام. این ربات برای ثبت رویدادهای کاری فروشگاه کالامپیچ است.\n"
    "رویدادها را به زبان ساده بنویسید؛ مثلاً: «فروش ۵۰۰ هزار تومان پسته» یا "
    "«خرید ۲ کیلو آرد ۳۰۰ هزار تومان».\n"
    "اگر اطلاعات لازم ناقص باشد، همین‌جا در چت خصوصی از شما می‌پرسم."
)

WELCOME_MANAGER = (
    "سلام. حساب شما به عنوان مدیر شناخته شد.\n"
    "برای گزارش دوره مشخص، بازه را بنویسید؛ مثلاً «گزارش فروش و هزینه دیروز» یا "
    "«گزارش این هفته».\n"
    "گزارش روزانه هر شب ساعت ۲۳:۳۰ به وقت تهران ارسال می‌شود."
)

ACCESS_DENIED = "دسترسی شما به این ربات فعال نیست. برای فعال‌سازی با مدیر فروشگاه هماهنگ کنید."

RECORDING_DISABLED = "ثبت رویداد برای حساب شما فعال نیست."

UNSUPPORTED_MEDIA = (
    "فعلاً فقط پیام متنی پشتیبانی می‌شود. لطفاً همین رویداد را در قالب متن بفرستید."
)

ACK_CONFIRMED = "✅ ثبت شد"
ACK_CORRECTED = "✅ اصلاح ثبت شد"

CORRECTION_NOT_ALLOWED = "شما اجازه اصلاح این رکورد را ندارید."
CORRECTION_NO_TARGET = "پیامی که به آن پاسخ داده‌اید به یک رویداد ثبت‌شده مربوط نیست."
REPLY_TARGET_NOT_AN_EVENT = CORRECTION_NO_TARGET
CORRECTION_ALREADY_RESOLVED = "این مورد قبلاً بسته شده است؛ اگر تغییر تازه‌ای هست، لطفاً آن را در یک پیام جدید بنویسید."
CORRECTION_NO_CHANGE = "در این پیام تغییری که بتوانم ثبت کنم پیدا نشد. لطفاً تغییر را روشن‌تر بنویسید."
CORRECTION_INVALID = "اصلاح ثبت نشد. این موارد را روشن کنید: {fields}"

CLARIFICATION_ALREADY_CLOSED = "این مورد قبلاً بسته شده است."
CLARIFICATION_MULTIPLE_OPEN = (
    "چند مورد در انتظار پاسخ است. لطفاً به همان پیامی که سؤال را پرسیده‌ام پاسخ بدهید."
)
CLARIFICATION_EXHAUSTED = (
    "این مورد ثبت نشد، چون اطلاعات لازم کامل نشد. اگر لازم بود، همان رویداد را "
    "کامل‌تر و در یک پیام تازه بفرستید."
)

REPORT_PERIOD_UNKNOWN = (
    "بازه گزارش مشخص نیست. لطفاً تاریخ شروع و پایان را بنویسید؛ "
    "مثلاً: 2025-12-01 تا 2025-12-05"
)
REPORT_RANGE_TOO_WIDE = "بازه درخواستی بیش از {days} روز است. لطفاً بازه کوتاه‌تری بگویید."
REPORT_FAILED = (
    "تهیه گزارش ممکن نشد. این مورد در گزارش عملیاتی ثبت می‌شود؛ لطفاً بعداً دوباره درخواست کنید."
)
REPORT_NOT_A_REPORT = "not_a_report"

FIELD_UNKNOWN_HINT = "اگر واقعاً نامشخص است، بنویسید «نامشخص»."

FIELD_LABELS = {
    "event_type": "نوع رویداد",
    "event_date": "تاریخ رویداد",
    "amount": "مبلغ",
    "currency": "واحد پول",
    "item_name": "نام کالا",
    "quantity": "مقدار",
    "unit": "واحد اندازه‌گیری",
    "inventory_direction": "نوع گردش موجودی",
    "description": "توضیح رویداد",
}

EVENT_TYPE_QUESTION = (
    "نوع این رویداد را مشخص کنید: فروش/درآمد، هزینه/خرید، گردش موجودی، "
    "یا یادداشت عملیاتی."
)


def _type_label(event_type: str | None) -> str:
    if not event_type:
        return "رویداد"
    return EVENT_TYPE_LABELS_FA.get(event_type, "رویداد")


def format_amount_with_currency(amount: str | None, currency: str | None) -> str:
    if amount is None:
        return "نامشخص"
    formatted = format_decimal(to_decimal(amount))
    return f"{formatted} {currency}" if currency else formatted


def describe_event(fields: dict) -> str:
    """One concise Persian line describing an event, for acknowledgements."""

    event_type = fields.get("event_type")
    label = _type_label(event_type)
    if event_type in {"sale_revenue", "expense_purchase"}:
        return f"{label}: {format_amount_with_currency(fields.get('amount'), fields.get('currency'))}"
    if event_type == "inventory_movement":
        direction = DIRECTION_LABELS_FA.get(fields.get("inventory_direction") or "", "")
        item = fields.get("item_name") or "کالا"
        quantity = fields.get("quantity") or "?"
        unit = fields.get("unit") or ""
        return f"{label} ({direction}) — {item}: {quantity} {unit}".strip()
    if event_type == "operational_note":
        description = fields.get("description") or "یادداشت"
        return f"{label}: {description}"
    return label


def ack_line(prefix: str, fields: dict, *, revision_number: int | None = None) -> str:
    lines = [f"{prefix} — {describe_event(fields)}"]
    if fields.get("event_date"):
        lines.append(f"تاریخ: {fields['event_date']}")
    if revision_number:
        lines.append(f"نسخه: {revision_number}")
    return "\n".join(lines)


def clarification_question(fields: dict, missing_fields: list[str]) -> str:
    """Deterministic question built only from validated missing fields.

    The application never asks the model to invent a question, so a question can
    never embed made-up facts or instructions from the source message.
    """

    lines: list[str] = []
    if "event_type" in missing_fields:
        lines.append(EVENT_TYPE_QUESTION)
        remaining = [field for field in missing_fields if field != "event_type"]
        if remaining:
            lines.append("همچنین این موارد را مشخص کنید:")
            lines.extend(f"• {FIELD_LABELS.get(field, field)}" for field in remaining)
    else:
        lines.append("برای ثبت این پیام در فهرست عملیات به این اطلاعات نیاز دارم:")
        lines.extend(f"• {FIELD_LABELS.get(field, field)}" for field in missing_fields)
    lines.append("لطفاً فقط همین موارد را در پاسخ به همین پیام بنویسید.")
    lines.append(FIELD_UNKNOWN_HINT)
    return "\n".join(lines)


def clarification_answered(fields: dict) -> str:
    return ack_line(ACK_CONFIRMED, fields)


def report_header(*, title: str, period_start: str, period_end: str, late: bool) -> str:
    lines = [
        f"📊 {title} — {period_start}" + (f" تا {period_end}" if period_end != period_start else ""),
        "بازه محاسبه: به وقت تهران",
    ]
    if late:
        lines.append("⚠️ این گزارش با تأخیر ارسال شده است.")
    return "\n".join(lines)


def report_section(title: str, lines: list[str]) -> str:
    return title + "\n" + "\n".join(lines)


def no_events_recorded() -> str:
    return "در این بازه هیچ رویدادی ثبت نشده است. این به معنی نبود فعالیت کاری نیست."


def pending_caveat(count: int) -> str:
    return f"{count} رکورد در انتظار شفاف‌سازی است و در جمع‌های بالا حساب نشده است."


def undated_pending_caveat(count: int) -> str:
    return (
        f"{count} رکورد در انتظار شفاف‌سازی است و تاریخ مشخصی ندارد؛ "
        "در جمع‌های این گزارش نیامده است."
    )


def rejected_caveat(count: int) -> str:
    return f"{count} رکورد رد شده است و در جمع‌های بالا حساب نشده است."


def missing_currency_caveat(count: int) -> str:
    return f"{count} رکورد مالی بدون واحد پول مشخص ثبت شده و در جمع‌های ارزی نیامده است."


def empty_totals_note() -> str:
    return "مبلغی با واحد پول مشخص ثبت نشده است."


DIRECTION_ORDER = (RECEIVED, CONSUMED, WASTED, ADJUSTMENT)
