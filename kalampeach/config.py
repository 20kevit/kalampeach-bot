"""Configuration loading and strict validation.

Design notes
------------
* Values come from environment variables, optionally seeded from a private
  ``.env`` file that must live outside the public web root.
* Validation collects *all* problems and raises :class:`ConfigError` with
  messages that never echo secret values.
* Placeholder values from ``.env.example`` are rejected so a half-configured
  deployment fails loudly instead of running with fake credentials.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time as clock_time
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from .timeutil import TimeError, load_timezone

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

WEBHOOK_PATH = "/telegram/webhook"
BUSINESS_TIMEZONE = "Asia/Tehran"
DAILY_REPORT_TIME = "23:30"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "your-",
    "your_",
    "placeholder",
    "replace-me",
    "replace_me",
    "<",
    ">",
    "example.com",
    "cpanel_user",
    "xxxx",
    "todo",
    "tbd",
)
_PUBLIC_DIR_NAMES = {"public_html", "public_www", "www", "public"}


class ConfigError(Exception):
    """Raised when the deployment configuration is incomplete or unsafe."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(dict.fromkeys(problems))
        super().__init__("configuration is invalid: " + "; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    telegram_bot_token: str
    telegram_group_id: int
    manager_telegram_id: int
    employee_ids: frozenset
    webhook_secret: str
    webhook_public_url: str
    nvidia_api_key: str
    nvidia_base_url: str
    nvidia_model: str
    database_path: Path
    business_timezone_name: str
    daily_report_time: clock_time
    default_currency: Optional[str] = None
    telegram_api_base_url: str = "https://api.telegram.org"
    nvidia_timeout_seconds: float = 15.0
    nvidia_max_retries: int = 1
    nvidia_response_format: str = "json_object"
    worker_batch_size: int = 20
    worker_max_seconds: float = 50.0
    worker_lease_seconds: int = 120
    worker_max_attempts: int = 5
    daily_report_recovery_hours: float = 6.0
    report_max_range_days: int = 62
    sqlite_busy_timeout_ms: int = 5000
    sqlite_journal_mode: str = "WAL"
    max_webhook_bytes: int = 262144
    log_level: str = "INFO"
    env_file: Optional[str] = None

    # -- derived helpers ---------------------------------------------------
    @property
    def webhook_path(self) -> str:
        return WEBHOOK_PATH

    @property
    def timezone(self):
        return load_timezone(self.business_timezone_name)

    def is_manager(self, telegram_user_id: Optional[int]) -> bool:
        return telegram_user_id is not None and int(telegram_user_id) == self.manager_telegram_id

    def can_record(self, telegram_user_id: Optional[int]) -> bool:
        """Recording allowlist. The manager may record only when listed."""

        if telegram_user_id is None:
            return False
        return int(telegram_user_id) in self.employee_ids

    def is_authorized(self, telegram_user_id: Optional[int]) -> bool:
        return self.is_manager(telegram_user_id) or self.can_record(telegram_user_id)

    def recording_ids(self) -> frozenset:
        return self.employee_ids

    def redaction_secrets(self) -> tuple:
        return (self.telegram_bot_token, self.nvidia_api_key, self.webhook_secret)


class _Collector:
    def __init__(self) -> None:
        self.problems: List[str] = []

    def add(self, message: str) -> None:
        self.problems.append(message)


def _raw(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if value is None:
        return ""
    return str(value).strip()


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _required_text(
    env: Mapping[str, str],
    key: str,
    collector: _Collector,
    *,
    secret: bool = False,
    max_length: int = 512,
) -> str:
    value = _raw(env, key)
    if not value:
        collector.add("%s is required" % key)
        return ""
    if _is_placeholder(value):
        collector.add("%s still contains a placeholder value from .env.example" % key)
    if len(value) > max_length:
        collector.add("%s is longer than %d characters" % (key, max_length))
    if any(ord(ch) < 32 for ch in value):
        collector.add("%s contains control characters" % key)
    if not secret and ("\n" in value or "\r" in value):
        collector.add("%s must be a single line" % key)
    return value


def _optional_text(
    env: Mapping[str, str],
    key: str,
    collector: _Collector,
    *,
    max_length: int = 512,
) -> str:
    value = _raw(env, key)
    if not value:
        return ""
    if _is_placeholder(value):
        collector.add("%s still contains a placeholder value from .env.example" % key)
    if len(value) > max_length:
        collector.add("%s is longer than %d characters" % (key, max_length))
    return value


def _parse_int(
    raw: str,
    key: str,
    collector: _Collector,
    *,
    required: bool,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    default: Optional[int] = None,
    allow_zero: bool = True,
) -> Optional[int]:
    if not raw:
        if required:
            return None  # requirement already reported by _required_text
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        collector.add("%s must be an integer" % key)
        return default
    if not allow_zero and value == 0:
        collector.add("%s must not be zero" % key)
    if minimum is not None and value < minimum:
        collector.add("%s must be at least %d" % (key, minimum))
    if maximum is not None and value > maximum:
        collector.add("%s must be at most %d" % (key, maximum))
    return value


def _parse_float(
    raw: str,
    key: str,
    collector: _Collector,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        collector.add("%s must be a number" % key)
        return default
    if value < minimum or value > maximum:
        collector.add("%s must be between %s and %s" % (key, minimum, maximum))
        return default
    return value


def _parse_id_list(raw: str, key: str, collector: _Collector) -> frozenset:
    if not raw:
        return frozenset()
    ids = set()
    for chunk in re.split(r"[,;\s]+", raw):
        piece = chunk.strip()
        if not piece:
            continue
        try:
            value = int(piece, 10)
        except ValueError:
            collector.add("%s must be a comma-separated list of numeric Telegram user IDs" % key)
            return frozenset()
        if value <= 0:
            collector.add("%s must contain positive numeric Telegram user IDs" % key)
            return frozenset()
        ids.add(value)
    return frozenset(ids)


def _validate_webhook_url(raw: str, collector: _Collector) -> str:
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        collector.add("WEBHOOK_PUBLIC_URL must be an https:// URL")
    if not parsed.hostname:
        collector.add("WEBHOOK_PUBLIC_URL must include a hostname")
    if parsed.query or parsed.fragment:
        collector.add("WEBHOOK_PUBLIC_URL must not include a query string or fragment")
    if not parsed.path.endswith(WEBHOOK_PATH):
        collector.add(
            "WEBHOOK_PUBLIC_URL must end with %s (the WSGI route the app exposes)" % WEBHOOK_PATH
        )
    if parsed.hostname and parsed.hostname in ("localhost", "127.0.0.1"):
        collector.add("WEBHOOK_PUBLIC_URL must be public: Telegram cannot reach localhost")
    return raw


def _validate_nvidia_base_url(raw: str, collector: _Collector) -> str:
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        collector.add("NVIDIA_BASE_URL must be an http(s) URL")
        return raw
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        collector.add("NVIDIA_BASE_URL must use https for a remote host")
    if not parsed.hostname:
        collector.add("NVIDIA_BASE_URL must include a hostname")
    if parsed.query or parsed.fragment:
        collector.add("NVIDIA_BASE_URL must not include a query string or fragment")
    return raw.rstrip("/")


def _validate_database_path(raw: str, collector: _Collector) -> Path:
    if not raw:
        return Path("")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        collector.add("DATABASE_PATH must be an absolute path")
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & _PUBLIC_DIR_NAMES:
        collector.add("DATABASE_PATH must live outside the public web root")
    if path.name in ("", ".", ".."):
        collector.add("DATABASE_PATH must point to a file")
    return path


def _parse_clock(raw: str, key: str, collector: _Collector) -> clock_time:
    if not re.match(r"^\d{2}:\d{2}$", raw or ""):
        collector.add("%s must use HH:MM format" % key)
        return clock_time(23, 30)
    hour, minute = (int(part) for part in raw.split(":"))
    if hour > 23 or minute > 59:
        collector.add("%s must be a valid 24-hour time" % key)
        return clock_time(23, 30)
    return clock_time(hour, minute)


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(["ENV_FILE could not be read: %s" % exc.__class__.__name__])
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            raise ConfigError(["ENV_FILE line %d is not KEY=VALUE" % number])
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ConfigError(["ENV_FILE line %d has an invalid key name" % number])
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _merged_env(env: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Environment variables, optionally seeded from a private .env file."""

    if env is not None:
        merged = {key: str(value) for key, value in env.items()}
        explicit_file = merged.get("ENV_FILE", "").strip()
        if explicit_file:
            path = Path(explicit_file)
            if path.is_file():
                for key, value in _parse_env_file(path).items():
                    merged.setdefault(key, value)
        return merged

    merged = {key: value for key, value in os.environ.items()}
    env_file = merged.get("ENV_FILE", "").strip()
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if path.is_file():
        file_values = _parse_env_file(path)
        for key, value in file_values.items():
            merged.setdefault(key, value)
    return merged


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Load and validate configuration.

    ``env=None`` reads the process environment (with an optional private
    ``.env`` file). Passing a mapping is intended for tests and for the
    ``doctor`` command's dry runs.
    """

    values = _merged_env(env)
    collector = _Collector()

    telegram_token = _required_text(values, "TELEGRAM_BOT_TOKEN", collector)
    group_raw = _required_text(values, "TELEGRAM_GROUP_ID", collector)
    manager_raw = _required_text(values, "MANAGER_TELEGRAM_ID", collector)
    employees_raw = _required_text(values, "EMPLOYEE_TELEGRAM_IDS", collector)
    webhook_secret = _required_text(values, "TELEGRAM_WEBHOOK_SECRET", collector, secret=True)
    webhook_url = _required_text(values, "WEBHOOK_PUBLIC_URL", collector)
    nvidia_key = _required_text(values, "NVIDIA_API_KEY", collector, secret=True)
    nvidia_base = _required_text(values, "NVIDIA_BASE_URL", collector)
    nvidia_model = _required_text(values, "NVIDIA_MODEL", collector, max_length=200)
    database_raw = _required_text(values, "DATABASE_PATH", collector)

    # Telegram numbers
    group_id = _parse_int(group_raw, "TELEGRAM_GROUP_ID", collector, required=True, allow_zero=False)
    manager_id = _parse_int(
        manager_raw, "MANAGER_TELEGRAM_ID", collector, required=True, minimum=1
    )
    employee_ids = _parse_id_list(employees_raw, "EMPLOYEE_TELEGRAM_IDS", collector)
    if not employee_ids:
        collector.add("EMPLOYEE_TELEGRAM_IDS must list at least one authorized recorder")

    if not telegram_token:
        pass
    elif not re.match(r"^\d{4,}:[A-Za-z0-9_-]{20,}$", telegram_token):
        collector.add("TELEGRAM_BOT_TOKEN does not look like a BotFather token")

    # Telegram API base (optional override)
    api_base = _optional_text(values, "TELEGRAM_API_BASE_URL", collector) or (
        "https://api.telegram.org"
    )
    api_parsed = urlparse(api_base)
    if api_parsed.scheme not in ("https", "http") or not api_parsed.hostname:
        collector.add("TELEGRAM_API_BASE_URL must be an http(s) URL with a hostname")
    api_base = api_base.rstrip("/")

    # Webhook secret charset per Telegram's documented requirement
    if webhook_secret and not re.match(r"^[A-Za-z0-9_-]{16,256}$", webhook_secret):
        collector.add(
            "TELEGRAM_WEBHOOK_SECRET must be 16-256 characters using only A-Z, a-z, 0-9, '_' or '-'"
        )

    webhook_public_url = _validate_webhook_url(webhook_url, collector)
    nvidia_base_url = _validate_nvidia_base_url(nvidia_base, collector)

    database_path = _validate_database_path(database_raw, collector)

    tz_name = _optional_text(values, "BUSINESS_TIMEZONE", collector) or BUSINESS_TIMEZONE
    if tz_name != BUSINESS_TIMEZONE:
        collector.add(
            "BUSINESS_TIMEZONE must be %s for the MVP (see DECISIONS.md)" % BUSINESS_TIMEZONE
        )
    try:
        load_timezone(tz_name)
    except TimeError as exc:
        collector.add(str(exc))

    report_time_raw = _optional_text(values, "DAILY_REPORT_TIME", collector) or DAILY_REPORT_TIME
    report_time = _parse_clock(report_time_raw, "DAILY_REPORT_TIME", collector)
    if report_time_raw and report_time_raw != DAILY_REPORT_TIME:
        collector.add(
            "DAILY_REPORT_TIME must be %s for the MVP; change the product decision first"
            % DAILY_REPORT_TIME
        )

    default_currency = _optional_text(values, "DEFAULT_CURRENCY", collector, max_length=16)
    if default_currency and re.fullmatch(r"[\d.,]+", default_currency):
        collector.add("DEFAULT_CURRENCY must be a currency label, not a number")

    log_level = (_optional_text(values, "LOG_LEVEL", collector) or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        collector.add("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    response_format = (
        _optional_text(values, "NVIDIA_RESPONSE_FORMAT", collector) or "json_object"
    ).lower()
    if response_format not in ("json_object", "none"):
        collector.add("NVIDIA_RESPONSE_FORMAT must be 'json_object' or 'none'")

    journal_mode = (_optional_text(values, "SQLITE_JOURNAL_MODE", collector) or "WAL").upper()
    if journal_mode not in ("WAL", "DELETE", "TRUNCATE", "PERSIST"):
        collector.add("SQLITE_JOURNAL_MODE must be WAL, DELETE, TRUNCATE, or PERSIST")

    settings = Settings(
        telegram_bot_token=telegram_token,
        telegram_group_id=group_id if group_id is not None else 0,
        manager_telegram_id=manager_id if manager_id is not None else 0,
        employee_ids=employee_ids,
        webhook_secret=webhook_secret,
        webhook_public_url=webhook_public_url,
        nvidia_api_key=nvidia_key,
        nvidia_base_url=nvidia_base_url,
        nvidia_model=nvidia_model,
        database_path=database_path,
        business_timezone_name=tz_name,
        daily_report_time=report_time,
        default_currency=default_currency or None,
        telegram_api_base_url=api_base,
        nvidia_timeout_seconds=_parse_float(
            values.get("NVIDIA_TIMEOUT_SECONDS", ""),
            "NVIDIA_TIMEOUT_SECONDS",
            collector,
            default=15.0,
            minimum=5.0,
            maximum=60.0,
        ),
        nvidia_max_retries=_parse_int(
            _raw(values, "NVIDIA_MAX_RETRIES"),
            "NVIDIA_MAX_RETRIES",
            collector,
            required=False,
            default=1,
            minimum=0,
            maximum=3,
        )
        or 0,
        nvidia_response_format=response_format,
        worker_batch_size=_parse_int(
            _raw(values, "WORKER_BATCH_SIZE"),
            "WORKER_BATCH_SIZE",
            collector,
            required=False,
            default=20,
            minimum=1,
            maximum=200,
        )
        or 20,
        worker_max_seconds=_parse_float(
            _raw(values, "WORKER_MAX_SECONDS"),
            "WORKER_MAX_SECONDS",
            collector,
            default=50.0,
            minimum=5.0,
            maximum=280.0,
        ),
        worker_lease_seconds=_parse_int(
            _raw(values, "WORKER_LEASE_SECONDS"),
            "WORKER_LEASE_SECONDS",
            collector,
            required=False,
            default=120,
            minimum=30,
            maximum=900,
        )
        or 120,
        worker_max_attempts=_parse_int(
            _raw(values, "WORKER_MAX_ATTEMPTS"),
            "WORKER_MAX_ATTEMPTS",
            collector,
            required=False,
            default=5,
            minimum=1,
            maximum=20,
        )
        or 5,
        daily_report_recovery_hours=_parse_float(
            _raw(values, "DAILY_REPORT_RECOVERY_HOURS"),
            "DAILY_REPORT_RECOVERY_HOURS",
            collector,
            default=6.0,
            minimum=0.5,
            maximum=48.0,
        ),
        report_max_range_days=_parse_int(
            _raw(values, "REPORT_MAX_RANGE_DAYS"),
            "REPORT_MAX_RANGE_DAYS",
            collector,
            required=False,
            default=62,
            minimum=1,
            maximum=366,
        )
        or 62,
        sqlite_busy_timeout_ms=_parse_int(
            _raw(values, "SQLITE_BUSY_TIMEOUT_MS"),
            "SQLITE_BUSY_TIMEOUT_MS",
            collector,
            required=False,
            default=5000,
            minimum=100,
            maximum=60000,
        )
        or 5000,
        sqlite_journal_mode=journal_mode,
        max_webhook_bytes=_parse_int(
            _raw(values, "MAX_WEBHOOK_BYTES"),
            "MAX_WEBHOOK_BYTES",
            collector,
            required=False,
            default=262144,
            minimum=1024,
            maximum=4194304,
        )
        or 262144,
        log_level=log_level,
        env_file=(values.get("ENV_FILE") or None) if env is None else None,
    )

    if manager_id is not None and manager_id in employee_ids:
        # Documented behavior: the manager can record events only when listed.
        pass

    if collector.problems:
        raise ConfigError(collector.problems)
    return settings
