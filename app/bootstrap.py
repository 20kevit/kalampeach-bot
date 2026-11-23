"""Process wiring: configuration → logging → database, AI provider, Telegram.

Used by the Passenger entry point, cron scripts and tests. Every collaborator
can be injected, which is why the test suite needs neither network access nor
credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from app.ai.nvidia import NvidiaProvider
from app.ai.provider import AIProvider
from app.config import Config, load_config
from app.context import AppContext
from app.db import Database
from app.logging_setup import configure_logging, get_logger
from app.telegram.client import TelegramClient
from app.timeutil import utc_now


def build_context(
    config: Config | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    ai: AIProvider | None = None,
    telegram: TelegramClient | None = None,
    clock: Callable[[], datetime] = utc_now,
    configure_logs: bool = True,
) -> AppContext:
    resolved = config or load_config(environ)
    if configure_logs:
        configure_logging(resolved.log_level, resolved.log_file)
    logger = get_logger()

    provider = ai or NvidiaProvider(
        api_key=resolved.nvidia_api_key,
        base_url=resolved.nvidia_base_url,
        model=resolved.nvidia_model,
        timeout_seconds=resolved.nvidia_timeout_seconds,
        max_retries=resolved.nvidia_max_retries,
        temperature=resolved.nvidia_temperature,
        json_mode=resolved.nvidia_json_mode,
    )
    client = telegram or TelegramClient(
        token=resolved.telegram_bot_token, base_url=resolved.telegram_api_base_url
    )

    logger.info(
        "context_ready",
        extra={
            "fields": {
                "timezone": resolved.business_timezone,
                "daily_report_time": resolved.daily_report_time,
                "schema_version_expected": 1,
            }
        },
    )
    return AppContext(
        config=resolved,
        db=Database(resolved.database_path),
        ai=provider,
        telegram=client,
        clock=clock,
        logger=logger,
    )
