"""Configuration loading and validation.

Values come from process environment variables, optionally seeded from a
private env file (``KALAMPEACH_ENV_FILE`` or ``ENV_FILE``). Real process
variables always win over the file, so a cPanel application environment and a
cron-safe env file can coexist.

Validation is strict and fail-closed: a missing or malformed required value
stops the application with a message that lists key *names* only. No secret
value, and no placeholder default, is ever invented.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import ConfigError

_MIN_WEBHOOK_SECRET_LENGTH = 16
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_REPORT_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# The product specification fixes the business timezone and report time.
REQUIRED_BUSINESS_TIMEZONE = "Asia/Tehran"
REQUIRED_DAILY_REPORT_TIME = "23:30"

_DEFAULT_ENV_FILES = ("KALAMPEACH_ENV_FILE", "ENV_FILE")


class _Problems(list):
    """Collects human-readable configuration problems."""

    def add(self, message: str) -> None:
        self.append(message)


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` env file.

    Supported: blank lines, ``#`` comments, optional ``export`` prefix, single
    or double quotes around the value. Interpolation is intentionally not
    supported so a ``$`` in a secret is never expanded.
    """

    values: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _merged_environ(environ: Mapping[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in _DEFAULT_ENV_FILES:
        candidate = (environ or os.environ).get(key)
        if candidate:
            path = Path(candidate)
            if path.is_file():
                merged.update(parse_env_file(path))
            else:
                raise ConfigError([f"{key} points to a file that does not exist"])
            break
    merged.update(dict(environ if environ is not None else os.environ))
    return merged


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    telegram_bot_token: str = field(repr=False)
    telegram_group_id: int
    manager_telegram_id: int
    employee_telegram_ids: frozenset[int]
    telegram_webhook_secret: str = field(repr=False)
    webhook_public_url: str
    telegram_api_base_url: str
    manager_can_record_events: bool
    send_group_acknowledgements: bool

    nvidia_api_key: str = field(repr=False)
    nvidia_base_url: str
    nvidia_model: str
    nvidia_timeout_seconds: float
    nvidia_max_retries: int
    nvidia_temperature: float
    nvidia_json_mode: bool

    business_timezone: str
    daily_report_time: str
    default_currency: str | None
    allow_message_date_as_event_date: bool
    report_narrative_enabled: bool
    report_catchup_days: int

    database_path: str
    log_file: str | None
    log_level: str

    worker_max_runtime_seconds: int
    worker_batch_size: int
    inbox_max_attempts: int
    report_max_attempts: int
    max_clarification_rounds: int
    max_report_range_days: int

    environment: str = "production"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)

    @property
    def report_hour_minute(self) -> tuple[int, int]:
        hour, minute = self.daily_report_time.split(":")
        return int(hour), int(minute)

    def can_record(self, telegram_user_id: int) -> bool:
        if telegram_user_id == self.manager_telegram_id:
            return self.manager_can_record_events or telegram_user_id in self.employee_telegram_ids
        return telegram_user_id in self.employee_telegram_ids

    def is_manager(self, telegram_user_id: int) -> bool:
        return telegram_user_id == self.manager_telegram_id


def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str:
    value = env.get(key)
    if value is None:
        return default or ""
    return value.strip()


def _required_text(env: Mapping[str, str], key: str, problems: _Problems) -> str:
    value = _get(env, key)
    if not value:
        problems.add(f"{key} is required but empty")
    return value


def _parse_int(
    env: Mapping[str, str],
    key: str,
    problems: _Problems,
    *,
    required: bool,
    default: int | None = None,
    minimum: int | None = None,
) -> int:
    raw = _get(env, key)
    if not raw:
        if required:
            problems.add(f"{key} is required but empty")
            return default if default is not None else 0
        return default if default is not None else 0
    try:
        value = int(raw)
    except ValueError:
        problems.add(f"{key} must be an integer")
        return default if default is not None else 0
    if minimum is not None and value < minimum:
        problems.add(f"{key} must be >= {minimum}")
    return value


def _parse_bool(
    env: Mapping[str, str], key: str, problems: _Problems, *, default: bool
) -> bool:
    raw = _get(env, key)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    problems.add(f"{key} must be true or false")
    return default


def _parse_float(
    env: Mapping[str, str],
    key: str,
    problems: _Problems,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = _get(env, key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        problems.add(f"{key} must be a number")
        return default
    if not (minimum <= value <= maximum):
        problems.add(f"{key} must be between {minimum} and {maximum}")
    return value


def _parse_id_list(env: Mapping[str, str], key: str, problems: _Problems) -> frozenset[int]:
    raw = _get(env, key)
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for chunk in re.split(r"[,;\s]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            problems.add(f"{key} contains a non-numeric Telegram user ID")
            continue
        if value <= 0:
            problems.add(f"{key} contains a Telegram user ID that is not positive")
            continue
        ids.add(value)
    return frozenset(ids)


def _validate_https_url(raw: str, key: str, problems: _Problems) -> str:
    if not raw:
        return raw
    if not raw.startswith("https://"):
        problems.add(f"{key} must be an https:// URL")
        return raw
    if len(raw) < len("https://a"):
        problems.add(f"{key} must include a host")
    return raw.rstrip("/")


def _validate_timezone(raw: str, problems: _Problems) -> str:
    value = raw or REQUIRED_BUSINESS_TIMEZONE
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        problems.add("BUSINESS_TIMEZONE is not a valid IANA timezone name")
        return value
    if value != REQUIRED_BUSINESS_TIMEZONE:
        problems.add(
            "BUSINESS_TIMEZONE must be Asia/Tehran; the product specification fixes "
            "business dates and the daily report to Asia/Tehran"
        )
    return value


def _validate_report_time(raw: str, problems: _Problems) -> str:
    value = raw or REQUIRED_DAILY_REPORT_TIME
    if not _REPORT_TIME_RE.match(value):
        problems.add("DAILY_REPORT_TIME must use 24-hour HH:MM format")
        return value
    if value != REQUIRED_DAILY_REPORT_TIME:
        problems.add(
            "DAILY_REPORT_TIME must be 23:30; the product specification fixes the "
            "daily manager report time to 23:30 Asia/Tehran"
        )
    return value


def _validate_database_path(raw: str, problems: _Problems) -> str:
    if not raw:
        return raw
    path = Path(raw)
    if not path.is_absolute():
        problems.add("DATABASE_PATH must be an absolute path")
    parts = {part.lower() for part in path.parts}
    if "public_html" in parts or "www" in parts:
        problems.add("DATABASE_PATH must not be inside a public web root")
    return str(path)


def _validate_log_file(raw: str, problems: _Problems) -> str | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        problems.add("LOG_FILE must be an absolute path or empty")
    parts = {part.lower() for part in path.parts}
    if "public_html" in parts:
        problems.add("LOG_FILE must not be inside a public web root")
    return str(path)


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Load and validate configuration, raising :class:`ConfigError` on failure."""

    env = _merged_environ(environ)
    problems = _Problems()

    token = _required_text(env, "TELEGRAM_BOT_TOKEN", problems)
    group_id = _parse_int(env, "TELEGRAM_GROUP_ID", problems, required=True)
    if group_id == 0 and _get(env, "TELEGRAM_GROUP_ID"):
        problems.add("TELEGRAM_GROUP_ID must not be 0")
    manager_id = _parse_int(env, "MANAGER_TELEGRAM_ID", problems, required=True, minimum=1)
    employees = _parse_id_list(env, "EMPLOYEE_TELEGRAM_IDS", problems)
    manager_can_record = _parse_bool(
        env, "MANAGER_CAN_RECORD_EVENTS", problems, default=False
    )

    secret = _required_text(env, "TELEGRAM_WEBHOOK_SECRET", problems)
    if secret and not _WEBHOOK_SECRET_RE.match(secret):
        problems.add(
            "TELEGRAM_WEBHOOK_SECRET must be 16-256 characters of A-Z, a-z, 0-9, _ or -"
        )

    webhook_url = _required_text(env, "WEBHOOK_PUBLIC_URL", problems)
    _validate_https_url(webhook_url, "WEBHOOK_PUBLIC_URL", problems)

    api_base = _validate_https_url(
        _get(env, "TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
        "TELEGRAM_API_BASE_URL",
        problems,
    )

    nvidia_key = _required_text(env, "NVIDIA_API_KEY", problems)
    nvidia_base = _required_text(env, "NVIDIA_BASE_URL", problems)
    nvidia_model = _required_text(env, "NVIDIA_MODEL", problems)
    _validate_https_url(nvidia_base, "NVIDIA_BASE_URL", problems)

    nvidia_timeout = _parse_float(
        env, "NVIDIA_TIMEOUT_SECONDS", problems, default=30.0, minimum=1.0, maximum=120.0
    )
    nvidia_retries = _parse_int(
        env, "NVIDIA_MAX_RETRIES", problems, required=False, default=2
    )
    if nvidia_retries < 0 or nvidia_retries > 5:
        problems.add("NVIDIA_MAX_RETRIES must be between 0 and 5")
    nvidia_temperature = _parse_float(
        env, "NVIDIA_TEMPERATURE", problems, default=0.0, minimum=0.0, maximum=2.0
    )
    nvidia_json_mode = _parse_bool(env, "NVIDIA_JSON_MODE", problems, default=False)

    timezone_name = _validate_timezone(_get(env, "BUSINESS_TIMEZONE"), problems)
    report_time = _validate_report_time(_get(env, "DAILY_REPORT_TIME"), problems)
    default_currency = _get(env, "DEFAULT_CURRENCY") or None
    if default_currency is not None and len(default_currency) > 16:
        problems.add("DEFAULT_CURRENCY must be 16 characters or fewer")

    database_path = _validate_database_path(
        _required_text(env, "DATABASE_PATH", problems), problems
    )
    log_file = _validate_log_file(_get(env, "LOG_FILE"), problems)
    log_level = (_get(env, "LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in _LOG_LEVELS:
        problems.add("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        log_level = "INFO"

    worker_runtime = _parse_int(
        env, "WORKER_MAX_RUNTIME_SECONDS", problems, required=False, default=50, minimum=5
    )
    worker_batch = _parse_int(
        env, "WORKER_BATCH_SIZE", problems, required=False, default=20, minimum=1
    )
    inbox_attempts = _parse_int(
        env, "INBOX_MAX_ATTEMPTS", problems, required=False, default=5, minimum=1
    )
    report_attempts = _parse_int(
        env, "REPORT_MAX_ATTEMPTS", problems, required=False, default=5, minimum=1
    )
    clarification_rounds = _parse_int(
        env, "MAX_CLARIFICATION_ROUNDS", problems, required=False, default=3, minimum=1
    )
    report_range = _parse_int(
        env, "MAX_REPORT_RANGE_DAYS", problems, required=False, default=31, minimum=1
    )
    catchup_days = _parse_int(
        env, "REPORT_CATCHUP_DAYS", problems, required=False, default=1, minimum=0
    )
    if catchup_days > 7:
        problems.add("REPORT_CATCHUP_DAYS must be 7 or fewer")

    allow_message_date = _parse_bool(
        env, "ALLOW_MESSAGE_DATE_AS_EVENT_DATE", problems, default=False
    )
    narrative_enabled = _parse_bool(
        env, "REPORT_NARRATIVE_ENABLED", problems, default=True
    )
    group_acks = _parse_bool(
        env, "SEND_GROUP_ACKNOWLEDGEMENTS", problems, default=True
    )

    # At least one identity must be able to record, otherwise the bot can never
    # capture anything and the deployment is certainly misconfigured.
    if not employees and not (manager_id and manager_can_record):
        problems.add(
            "No user can record events: set EMPLOYEE_TELEGRAM_IDS or enable "
            "MANAGER_CAN_RECORD_EVENTS for the manager"
        )

    if problems:
        raise ConfigError(list(problems))

    return Config(
        telegram_bot_token=token,
        telegram_group_id=group_id,
        manager_telegram_id=manager_id,
        employee_telegram_ids=employees,
        telegram_webhook_secret=secret,
        webhook_public_url=webhook_url,
        telegram_api_base_url=api_base,
        manager_can_record_events=manager_can_record,
        send_group_acknowledgements=group_acks,
        nvidia_api_key=nvidia_key,
        nvidia_base_url=nvidia_base,
        nvidia_model=nvidia_model,
        nvidia_timeout_seconds=nvidia_timeout,
        nvidia_max_retries=nvidia_retries,
        nvidia_temperature=nvidia_temperature,
        nvidia_json_mode=nvidia_json_mode,
        business_timezone=timezone_name,
        daily_report_time=report_time,
        default_currency=default_currency,
        allow_message_date_as_event_date=allow_message_date,
        report_narrative_enabled=narrative_enabled,
        report_catchup_days=catchup_days,
        database_path=database_path,
        log_file=log_file,
        log_level=log_level,
        worker_max_runtime_seconds=worker_runtime,
        worker_batch_size=worker_batch,
        inbox_max_attempts=inbox_attempts,
        report_max_attempts=report_attempts,
        max_clarification_rounds=clarification_rounds,
        max_report_range_days=report_range,
    )


REQUIRED_CONFIG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_GROUP_ID",
    "MANAGER_TELEGRAM_ID",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_PUBLIC_URL",
    "NVIDIA_API_KEY",
    "NVIDIA_BASE_URL",
    "NVIDIA_MODEL",
    "DATABASE_PATH",
)
"""Owner-supplied values without which the bot must refuse to run."""
