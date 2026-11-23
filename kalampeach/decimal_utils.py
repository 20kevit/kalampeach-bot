"""Decimal-safe money and quantity handling.

Money is stored as TEXT in SQLite and handled as :class:`decimal.Decimal` in
code. Floats are never accepted for monetary values, ``None`` (unknown) is never
coerced to zero, and quantities keep the unit exactly as stated by staff.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

MAX_SCALE = 4

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_MAP = {ord(ch): str(index) for index, ch in enumerate(_PERSIAN_DIGITS)}
_DIGIT_MAP.update({ord(ch): str(index) for index, ch in enumerate(_ARABIC_DIGITS)})

# Thousands separators used in Persian/Arabic and Latin writing.
_SEPARATORS = ",٬، "
_DECIMAL_SEPARATORS = ("٫",)  # Arabic decimal separator

_AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,%d})?$" % MAX_SCALE)


class DecimalValueError(ValueError):
    """Raised when a value cannot be interpreted as a lossless decimal."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__("%s: %s" % (field, reason))


def normalize_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII digits."""

    if not isinstance(text, str):
        return text
    return text.translate(_DIGIT_MAP)


def strip_separators(text: str) -> str:
    cleaned = text
    for separator in _SEPARATORS:
        cleaned = cleaned.replace(separator, "")
    for separator in _DECIMAL_SEPARATORS:
        cleaned = cleaned.replace(separator, ".")
    return cleaned


def parse_decimal(
    value: object,
    *,
    field: str,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    """Parse a stored/AI-supplied decimal string without losing precision.

    Only strings (or integral Python ints) are accepted. ``float`` input is
    rejected outright so a binary floating-point money value can never enter the
    database through this function.
    """

    if isinstance(value, bool):
        raise DecimalValueError(field, "boolean is not a number")
    if isinstance(value, float):
        raise DecimalValueError(field, "floating-point money values are not accepted")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, str):
        text = strip_separators(normalize_digits(value.strip()))
        if not text:
            raise DecimalValueError(field, "empty number")
        if not re.match(r"^[+-]?\d+(?:\.\d+)?$", text):
            raise DecimalValueError(field, "not a plain decimal number")
        try:
            candidate = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise DecimalValueError(field, "not a decimal number") from exc
    else:
        raise DecimalValueError(field, "unsupported number type")

    if not candidate.is_finite():
        raise DecimalValueError(field, "number must be finite")
    if candidate.as_tuple().exponent < -MAX_SCALE:
        raise DecimalValueError(field, "more than %d decimal places" % MAX_SCALE)
    if allow_negative is False and candidate < 0:
        raise DecimalValueError(field, "negative value is not allowed")
    if not allow_zero and candidate == 0:
        raise DecimalValueError(field, "value must be greater than zero")
    return candidate


def is_plain_decimal_string(value: object) -> bool:
    """True when ``value`` is a canonical, non-negative decimal string.

    Used by the AI schema validator, which must reject JSON numbers (floats).
    """

    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or text != value:
        return False
    return bool(_AMOUNT_RE.match(text))


def decimal_to_storage(value: Decimal) -> str:
    """Serialize a Decimal for storage as a plain decimal string."""

    if not isinstance(value, Decimal):
        raise DecimalValueError("value", "not a Decimal")
    if not value.is_finite():
        raise DecimalValueError("value", "number must be finite")
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def format_amount(value: Decimal) -> str:
    """Human-readable amount with thousands separators and no float rounding."""

    text = decimal_to_storage(value)
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if "." in text:
        whole, fraction = text.split(".", 1)
        return "%s%s.%s" % (sign, _group(whole), fraction)
    return sign + _group(text)


def _group(digits: str) -> str:
    if not digits:
        return "0"
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return ",".join(out)


def sum_decimals(values: Iterable[Decimal]) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += value
    return total


def safe_optional_decimal(value: Optional[str], *, field: str) -> Optional[Decimal]:
    """Parse an optional stored decimal, raising on corrupt data."""

    if value is None or value == "":
        return None
    return parse_decimal(value, field=field)
