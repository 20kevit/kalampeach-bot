"""All user-facing Persian strings in one place.

Keeping copy out of the flow code makes review (and future wording changes)
straightforward. Numbers use Latin digits and ISO dates so that a message can be
traced back to the database record without ambiguity.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from ..ai.types import ExtractionResult
from ..decimal_utils import format_amount

EVENT_TYPE_LABELS = {
    "sale_revenue": "فروش/درآمد",
    "expense_purchase": "هزینه/خرید",
    "inventory_movement": "حرکت موجودی",
    "operational_note": "یادداشت عملیاتی",
}

INVENTORY_DIRECTION_LABELS = {
    "received": "دریافت",
    "consumed": "مصرف",
    "wasted": "ضایعات",
    "adjustment": "اصلاح",
}

FIELD_LABELS = {
    "event_type": "نوع رویداد",
    "event_date": "تاریخ",
    "amount": "مبلغ",
    "currency": "واحد پول (مثلاً تومان)",
    "item_name": "نام کالا",
    "quantity": "مقدار",
    "unit": "واحد (مثلاً عدد یا کیلوگرم)",
    "inventory_direction": "نوع حرکت موجودی (دریافت/مصرف/ضایعات/اصلاح)",
    "description": "توضیح کوتاه",
}

ACCESS_DENIED = (
    "دسترسی شما برای ثبت رویداد در این ربات فعال نیست. "
    "اگر باید اطلاعات کسب‌وکار ثبت کنید، شناسهٔ عددی تلگرام خود را به مدیر بدهید."
)

UNSUPPORTED_MEDIA = (
    "فعلاً فقط پیام متنی پشتیبانی می‌شود. محتوای این پیام (عکس، صدا، ویدیو یا فایل) "
    "خوانده و ثبت نشد."
)

MANAGER_ONLY = "گزارش‌ها فقط برای مدیر ارسال می‌شود."

MANAGER_CANNOT_RECORD = (
    "برای ثبت رویداد، شناسهٔ عددی شما باید در فهرست ثبت‌کنندگان تنظیم شده باشد. "
    "برای درخواست گزارش می‌توانید در چت خصوصی بنویسید."
)

REPORT_ONLY_IN_PRIVATE = (
    "گزارش‌ها فقط در چت خصوصی ارسال می‌شوند؛ لطفاً همین درخواست را در چت خصوصی ربات بنویسید."
)

UNCLEAR_MESSAGE = (
    "متوجه نشدم این پیام مربوط به ثبت فروش، هزینه، موجودی یا یک یادداشت کاری است یا نه. "
    "اگر رویداد کسب‌وکاری است، آن را با مبلغ و واحد پول (یا مقدار و واحد کالا) دوباره بنویسید."
)

GROUP_SETUP_HINT = (
    "برای پیگیری این مورد، لطفاً یک‌بار ربات را در چت خصوصی باز کنید و دکمهٔ Start را بزنید."
)

CORRECTION_APPLIED = "✅ اصلاح ثبت شد."

CORRECTION_NOT_ALLOWED = (
    "فقط ثبت‌کنندهٔ همان رویداد یا مدیر می‌تواند آن را اصلاح کند."
)

CORRECTION_UNCLEAR = (
    "متوجه تغییر موردنظر نشدم. لطفاً بنویسید کدام مقدار و به چه عددی اصلاح شود."
)

REPLY_TARGET_UNRESOLVED = (
    "پیامی که به آن پاسخ داده‌اید به رویدادی متصل نیست. لطفاً به پیام اصلی همان رویداد "
    "یا پیام تأیید ربات پاسخ دهید."
)

CLARIFICATION_ANSWER_RECORDED = "✅ پاسخ شما ثبت شد."

CLARIFICATION_EXPIRED = (
    "این مورد همچنان ناقص است و تا تکمیل اطلاعات در وضعیت «در انتظار شفاف‌سازی» می‌ماند. "
    "در گزارش‌ها به‌صورت جداگانه نمایش داده می‌شود."
)

REPORT_PERIOD_QUESTION = (
    "بازهٔ گزارش مشخص نیست. لطفاً تاریخ یا بازه را بنویسید؛ "
    "مثال: 2026-11-20 یا از 2026-11-01 تا 2026-11-15."
)

REPORT_RANGE_INVALID = (
    "بازهٔ درخواستی معتبر نیست (پایان باید بعد از شروع باشد و بازه بیش از حد طولانی نباشد). "
    "لطفاً دوباره بنویسید."
)

REPORT_FAILED = "تهیهٔ گزارش ممکن نشد. این خطا ثبت شد و بعداً دوباره تلاش می‌شود."
REPORT_DELIVERY_UNKNOWN = (
    "وضعیت ارسال بخشی از گزارش نامشخص است؛ برای جلوگیری از ارسال تکراری، ارسال خودکار "
    "متوقف شد. لطفاً گزارش را دوباره درخواست کنید."
)

REPORT_PERIOD_GIVEUP = (
    "بازهٔ گزارش مشخص نشد؛ برای جلوگیری از گزارش حدسی، درخواست لغو شد. "
    "لطفاً با تاریخ مشخص دوباره بنویسید."
)

LATE_REPORT_NOTE = "این گزارش با تأخیر ارسال می‌شود."


def event_type_label(event_type: Optional[str]) -> str:
    return EVENT_TYPE_LABELS.get(str(event_type or ""), str(event_type or "نامشخص"))


def direction_label(direction: Optional[str]) -> str:
    return INVENTORY_DIRECTION_LABELS.get(str(direction or ""), str(direction or ""))


def field_labels(fields: Sequence[str]) -> str:
    return "\n".join("- " + FIELD_LABELS.get(field, field) for field in fields)


def clarification_question(missing_fields: Sequence[str], *, prefix: Optional[str] = None) -> str:
    head = prefix or "برای ثبت این مورد به اطلاعات بیشتری نیاز است:"
    tail = "لطفاً فقط همین موارد را در پاسخ به همین پیام بنویسید."
    return "%s\n%s\n%s" % (head, field_labels(missing_fields), tail)


def confirmation_text(snapshot: Mapping[str, Optional[str]], *, clarification_note: str = "") -> str:
    """Short private confirmation of a recorded event."""

    lines = ["✅ ثبت شد", "نوع: %s" % event_type_label(snapshot.get("event_type"))]
    if snapshot.get("amount") is not None:
        amount = format_amount(_decimal_text(snapshot.get("amount")))
        currency = snapshot.get("currency") or "بدون واحد پول مشخص"
        lines.append("مبلغ: %s %s" % (amount, currency))
    if snapshot.get("quantity") is not None:
        lines.append(
            "مقدار: %s %s"
            % (format_amount(_decimal_text(snapshot.get("quantity"))), snapshot.get("unit") or "")
        )
    if snapshot.get("item_name"):
        lines.append("کالا: %s" % snapshot.get("item_name"))
    if snapshot.get("inventory_direction"):
        lines.append("حرکت: %s" % direction_label(snapshot.get("inventory_direction")))
    if snapshot.get("description"):
        lines.append("توضیح: %s" % snapshot.get("description"))
    lines.append("تاریخ: %s" % snapshot.get("event_date"))
    if clarification_note:
        lines.append(clarification_note)
    return "\n".join(lines)


def extraction_hint(extraction: ExtractionResult) -> str:
    """Internal (never user-facing) one-line summary for logs."""

    return "classification=%s event_type=%s missing=%s" % (
        extraction.classification,
        extraction.event_type,
        ",".join(extraction.missing_fields) or "-",
    )


def _decimal_text(value: Optional[str]):
    from decimal import Decimal

    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensive, storage is validated
        return Decimal("0")
