"""UTC storage plus Asia/Tehran business-date helpers.

Rules enforced by this module and used everywhere else:

* every stored timestamp is an ISO-8601 UTC string ending in ``Z``;
* business dates and the daily report boundary are computed in the configured
  IANA timezone (``Asia/Tehran`` by specification), never in the host or cron
  timezone;
* all conversions go through :mod:`zoneinfo`, so DST/historical offset changes
  are handled by the tz database rather than by a fixed offset.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_iso(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def local_datetime(value: datetime, tz: ZoneInfo) -> datetime:
    return ensure_utc(value).astimezone(tz)


def local_date(value: datetime, tz: ZoneInfo) -> date:
    return local_datetime(value, tz).date()


def local_time(value: datetime, tz: ZoneInfo) -> time:
    return local_datetime(value, tz).time().replace(tzinfo=None)


def parse_local_date(text: str) -> date:
    return date.fromisoformat(text)


def local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant of ``day`` 00:00 local time."""

    return datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(UTC)


def local_cutoff_utc(day: date, tz: ZoneInfo, hour: int, minute: int) -> datetime:
    """The UTC instant when ``day``'s daily report cutoff occurs (e.g. 23:30)."""

    return datetime.combine(day, time(hour, minute), tzinfo=tz).astimezone(UTC)


def teheran_today(now: datetime, tz: ZoneInfo) -> date:
    return local_date(now, tz)


def day_bounds_for_period(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Inclusive local-date period expressed as a half-open UTC interval."""

    start_utc = local_midnight_utc(start, tz)
    end_utc = local_midnight_utc(end + timedelta(days=1), tz)
    return start_utc, end_utc


def format_local_date(day: date) -> str:
    return day.isoformat()


def human_local_date(day: date) -> str:
    """Gregorian ISO date plus the Persian weekday name, for user messages."""

    names = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
    return f"{day.isoformat()} ({names[day.weekday()]})"
