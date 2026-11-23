"""Application context: the dependency bundle passed to services.

Explicit injection keeps every service testable without live Telegram, NVIDIA
or a real clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.ai.provider import AIProvider
from app.config import Config
from app.db import Database
from app.logging_setup import get_logger
from app.telegram.client import TelegramClient
from app.timeutil import utc_now


@dataclass
class AppContext:
    config: Config
    db: Database
    ai: AIProvider
    telegram: TelegramClient
    clock: Callable[[], datetime] = utc_now
    logger: logging.Logger = field(default_factory=lambda: get_logger())

    def now(self) -> datetime:
        return self.clock()
