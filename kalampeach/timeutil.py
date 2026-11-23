"""UTC storage helpers and Asia/Tehran business-date handling.

Rules enforced here (see DATA-MODEL.md and PRODUCT-SPEC.md section 9):

* every stored timestamp is UTC ISO-8601 with a trailing ``Z`` and second
  precision, so string comparison equals chronological comparison;
* business dates and daily-report boundaries are computed in the configured
  IANA timezone (``Asia/Tehran`` in the MVP), never in the host's local zone and
  never with a hardcoded offset;
* daylight-saving transitions are handled by ``zoneinfo`` only.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

_ISO_SECONDS = "%Y-%m-%dT%H:%M:%SZ"


class TimeError(ValueError):
    """Raised when a stored or supplied timestamp cannot be interpreted."""


def utc_now() -> datetime:
    """Current time as an aware UTC ``datetime`` with second precision."""

    return datetime.now(UTC).replace(microsecond=0)


def to_utc_iso(moment: datetime) -> str:
    """Serialize an aware datetime as canonical UTC ISO-8601 (``...Z``)."""

    if moment.tzinfo is None:
        raise TimeError("naive datetime cannot be stored; use an aware UTC datetime")
    return moment.astimezone(UTC).replace(microsecond=0).strftime(_ISO_SECONDS)


def parse_utc_iso(value: str) -> datetime:
    """Parse a canonical UTC timestamp produced by :func:`to_utc_iso`."""

    if not isinstance(value, str) or not value:
        raise TimeError("empty timestamp")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, _ISO_SECONDS).replace(tzinfo=UTC)
    except ValueError:
        # Tolerate full ISO-8601 with an explicit offset, e.g. from older rows.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:  # pragma: no cover - defensive
            raise TimeError("unsupported timestamp format") from exc
        if parsed.tzinfo is None:
            raise TimeError("timestamp has no timezone")
        parsed = parsed.astimezone(UTC)
    return parsed.replace(microsecond=0)


def load_timezone(name: str) -> ZoneInfo:
    """Load an IANA timezone or raise a clear, actionable error."""

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - host dependent
        raise TimeError(
            "IANA timezone data for %r is not available on this host; install the "
            "system tzdata package or pin the optional 'tzdata' wheel" % (name,)
        ) from exc


def local_datetime(moment: datetime, tz: ZoneInfo) -> datetime:
    """Convert an aware UTC datetime to local (business) time."""

    if moment.tzinfo is None:
        raise TimeError("naive datetime has no timezone")
    return moment.astimezone(tz)


def local_date(moment: datetime, tz: ZoneInfo) -> date:
    """Business date (Tehran calendar day) of an instant."""

    return local_datetime(moment, tz).date()


def local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """UTC instant of ``00:00`` local time on ``day``."""

    return datetime.combine(day, time(0, 0)).replace(tzinfo=tz).astimezone(UTC)


def local_time_to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    """UTC instant of a local wall-clock time on ``day``."""

    return datetime.combine(day, at).replace(tzinfo=tz).astimezone(UTC)


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def parse_business_date(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` business date, rejecting anything else."""

    if not isinstance(value, str) or len(value) != 10:
        raise TimeError("business date must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise TimeError("business date must be a real calendar date") from exc
    return parsed


def format_business_date(value: date) -> str:
    return value.isoformat()


def parse_unix_seconds(value: object) -> Optional[datetime]:
    """Convert a Telegram ``date`` field (unix seconds) to aware UTC."""

    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def local_date_range(start: date, end: date) -> list:
    """Inclusive list of dates; bounded by the caller."""

    if end < start:
        return []
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days
