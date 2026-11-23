"""Application context: settings, database, AI provider, Telegram client, clock.

Everything the services need is injected here, which keeps the business logic
testable without cPanel, Telegram, NVIDIA, or the system clock.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..ai.nvidia import NvidiaProvider
from ..ai.types import AIProvider
from ..config import Settings
from ..db import Database
from ..repository import record_ai_call
from ..telegram.client import TelegramClient
from ..timeutil import local_date, load_timezone, to_utc_iso, utc_now

Clock = Callable[[], datetime]


def default_worker_id() -> str:
    return "%s:%d:%s" % (socket.gethostname()[:32], os.getpid(), uuid.uuid4().hex[:8])


@dataclass
class AppContext:
    settings: Settings
    db: Database
    ai: AIProvider
    telegram: object
    clock: Clock = utc_now
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kalampeach"))
    worker_id: str = field(default_factory=default_worker_id)

    # -- time helpers ------------------------------------------------------
    def now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return moment

    def now_iso(self) -> str:
        return to_utc_iso(self.now())

    @property
    def timezone(self):
        return load_timezone(self.settings.business_timezone_name)

    def today_local(self) -> str:
        return local_date(self.now(), self.timezone).isoformat()

    def local_date_of(self, moment: datetime) -> str:
        return local_date(moment, self.timezone).isoformat()


def make_ai_recorder(db: Database, model: Optional[str], logger: logging.Logger):
    """Persist minimal AI call metadata (never prompts or message text)."""

    def recorder(operation: str, outcome: str, category: Optional[str], latency_ms: Optional[int]) -> None:
        try:
            with db.transaction() as connection:
                record_ai_call(
                    connection,
                    operation=operation,
                    provider="nvidia",
                    model=model,
                    outcome=outcome,
                    error_category=category,
                    latency_ms=latency_ms,
                    now=to_utc_iso(utc_now()),
                )
        except Exception as exc:  # pragma: no cover - audit is best effort
            logger.warning("ai_audit_failed error=%s", exc.__class__.__name__)

    return recorder


def build_database(settings: Settings) -> Database:
    return Database(
        settings.database_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        journal_mode=settings.sqlite_journal_mode,
    )


def build_context(
    settings: Settings,
    *,
    db: Optional[Database] = None,
    ai: Optional[AIProvider] = None,
    telegram: Optional[object] = None,
    clock: Optional[Clock] = None,
    logger: Optional[logging.Logger] = None,
) -> AppContext:
    log = logger or logging.getLogger("kalampeach")
    database = db or build_database(settings)
    provider = ai or NvidiaProvider(
        settings, recorder=make_ai_recorder(database, settings.nvidia_model, log)
    )
    client = telegram if telegram is not None else TelegramClient(settings)
    return AppContext(
        settings=settings,
        db=database,
        ai=provider,
        telegram=client,
        clock=clock or utc_now,
        logger=log,
    )
