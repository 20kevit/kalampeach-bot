"""Decimal-safe money and quantity handling.

Money is never stored or summed as a binary float. Values are validated as
``Decimal`` and stored as canonical decimal strings (``"1500000.5"``), which
SQLite preserves exactly. Aggregation happens in Python with ``Decimal`` so a
report figure is always reproducible from the stored rows.

Currency labels are preserved verbatim (normalised only for surrounding and
repeated whitespace). The application never guesses, converts or merges
different currency labels.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

MAX_DECIMAL_DIGITS = 18
MAX_DECIMAL_PLACES = 6


class AmountError(ValueError):
    """A money/quantity value is not a valid positive decimal."""


def parse_decimal(value: object, field: str = "amount", *, allow_zero: bool = False) -> Decimal:
    """Validate a decimal from an AI payload or a correction patch.

    Accepts ``str`` and ``int`` only. Floats are rejected on purpose: a float in
    the pipeline means somebody bypassed the decimal contract.
    """

    if isinstance(value, bool):
        raise AmountError(f"{field} must be a decimal string")
    if isinstance(value, float):
        raise AmountError(f"{field} must not be a float")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("+"):
            text = text[1:]
        text = text.replace(",", "").replace("٬", "").replace("،", "")
    else:
        raise AmountError(f"{field} must be a decimal string")

    if not text:
        raise AmountError(f"{field} is empty")
    try:
        decimal_value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise AmountError(f"{field} is not a valid decimal") from exc
    if not decimal_value.is_finite():
        raise AmountError(f"{field} must be finite")
    if decimal_value < 0:
        raise AmountError(f"{field} must not be negative")
    if decimal_value == 0 and not allow_zero:
        raise AmountError(f"{field} must be greater than zero")

    sign, digits, exponent = decimal_value.as_tuple()
    digit_count = len(digits)
    if digit_count > MAX_DECIMAL_DIGITS:
        raise AmountError(f"{field} has too many digits")
    if exponent < -MAX_DECIMAL_PLACES:
        raise AmountError(f"{field} has too many decimal places")
    return decimal_value


def canonical_decimal(value: Decimal) -> str:
    """Canonical string form: no exponent, no trailing zeros, no ``+``."""

    normalised = value.normalize()
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    return text


def canonical_amount(value: str | int | Decimal, field: str = "amount") -> str:
    return canonical_decimal(parse_decimal(value, field))


def canonical_quantity(value: str | int | Decimal, field: str = "quantity") -> str:
    return canonical_decimal(parse_decimal(value, field))


def to_decimal(value: str) -> Decimal:
    """Convert a value previously validated by :func:`canonical_decimal`."""

    return Decimal(value)


def format_decimal(value: Decimal) -> str:
    """Group thousands but keep the exact stored precision."""

    text = canonical_decimal(value)
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    integer_part, _, fraction = text.partition(".")
    grouped = f"{int(integer_part):,}"
    return f"{sign}{grouped}.{fraction}" if fraction else f"{sign}{grouped}"


def normalise_currency(value: object) -> str | None:
    """Preserve a currency label while removing accidental whitespace."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise AmountError("currency must be a string")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > 16:
        raise AmountError("currency label is longer than 16 characters")
    if any(ord(char) < 32 for char in text):
        raise AmountError("currency label contains control characters")
    return text
