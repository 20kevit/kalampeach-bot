"""Shared pytest fixtures.

Everything runs offline: a temporary SQLite database, a scripted fake AI
provider, a recording fake Telegram client, and a controllable clock. No test
needs a live Telegram token, an NVIDIA key, cPanel, or network access.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kalampeach.ai.types import (  # noqa: E402
    AIError,
    AIInvalidOutput,
    AIUnavailable,
    ClarificationAnswerResult,
    CorrectionResult,
    ExtractionResult,
    NarrativeResult,
    ReportRequestResult,
)
from kalampeach.config import load_settings  # noqa: E402
from kalampeach.db import Database  # noqa: E402
from kalampeach.migrations import ensure_schema  # noqa: E402
from kalampeach.services.context import AppContext  # noqa: E402

# A syntactically valid, obviously fake configuration. Not real credentials.
BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAFtestTokenNotReal_0123456789abc",
    "TELEGRAM_GROUP_ID": "-1001234567890",
    "MANAGER_TELEGRAM_ID": "9999",
    "EMPLOYEE_TELEGRAM_IDS": "1111,2222",
    "TELEGRAM_WEBHOOK_SECRET": "test_webhook_secret_0123456789",
    "WEBHOOK_PUBLIC_URL": "https://bot.kalampeach.test/telegram/webhook",
    "NVIDIA_API_KEY": "nvapi-fake-key-not-real-0001",
    "NVIDIA_BASE_URL": "https://ai.kalampeach.test/v1",
    "NVIDIA_MODEL": "test-model-v1",
    "BUSINESS_TIMEZONE": "Asia/Tehran",
    "DAILY_REPORT_TIME": "23:30",
    "DEFAULT_CURRENCY": "",
    "LOG_LEVEL": "WARNING",
}

GROUP_ID = -1001234567890
MANAGER_ID = 9999
EMPLOYEE_ID = 1111
OTHER_EMPLOYEE_ID = 2222
OUTSIDER_ID = 7777


def build_env(**overrides) -> dict:
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


@pytest.fixture
def env() -> dict:
    return dict(BASE_ENV)


@pytest.fixture
def settings(tmp_path, env):
    env = dict(env)
    env["DATABASE_PATH"] = str(tmp_path / "private" / "kalampeach_bot.sqlite3")
    return load_settings(env)


@pytest.fixture
def db(settings):
    database = Database(
        settings.database_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        journal_mode="WAL",
    )
    with database.connection() as connection:
        ensure_schema(connection)
    return database


class MutableClock:
    """Deterministic, adjustable clock."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def set(self, moment: datetime) -> None:
        self.moment = moment

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


@pytest.fixture
def clock() -> MutableClock:
    # 2026-11-23 12:00 UTC == 15:30 Asia/Tehran (well before the daily report).
    return MutableClock(datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc))


class FakeTelegram:
    """Recording Telegram client with scripted failures."""

    def __init__(self) -> None:
        self.sent = []
        self.attempts = []
        self.next_id = 5000
        self.failures = []  # raised (in order) regardless of chat
        self.chat_failures = {}  # chat_id -> list of exceptions

    def queue_failure(self, error: Exception, *, chat_id=None) -> None:
        if chat_id is None:
            self.failures.append(error)
        else:
            self.chat_failures.setdefault(int(chat_id), []).append(error)

    def send_message(self, chat_id, text, *, reply_to_message_id=None, disable_notification=False):
        self.attempts.append(
            {
                "chat_id": int(chat_id),
                "text": text,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        pending = self.chat_failures.get(int(chat_id))
        if pending:
            raise pending.pop(0)
        if self.failures:
            raise self.failures.pop(0)
        self.next_id += 1
        self.sent.append(
            {
                "chat_id": int(chat_id),
                "text": text,
                "message_id": self.next_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return self.next_id

    # convenience for assertions
    def texts_to(self, chat_id) -> list:
        return [item["text"] for item in self.sent if item["chat_id"] == int(chat_id)]

    def all_text(self) -> str:
        return "\n".join(item["text"] for item in self.sent)


class FakeAI:
    """Scripted AI provider.

    Tests queue results per operation. An unexpected call fails loudly, which is
    how "unauthorized messages are never sent to the model" is asserted.
    """

    name = "fake"

    def __init__(self) -> None:
        from collections import deque

        self.extractions = deque()
        self.corrections = deque()
        self.report_requests = deque()
        self.narratives = deque()
        self.clarification_answers = deque()
        self.calls = []
        self.fail_once = {}
        self.always_fail = {}
        self.requests = []

    def _next(self, queue, operation, request):
        self.calls.append(operation)
        self.requests.append((operation, request))
        if operation in self.always_fail:
            raise self.always_fail[operation]
        if operation in self.fail_once and self.fail_once[operation]:
            error = self.fail_once[operation].pop(0)
            raise error
        if not queue:
            raise AssertionError("unexpected AI call: %s" % operation)
        result = queue.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def extract_event(self, request):
        return self._next(self.extractions, "extract_event", request)

    def interpret_correction(self, request):
        return self._next(self.corrections, "interpret_correction", request)

    def complete_clarification(self, request):
        return self._next(self.clarification_answers, "complete_clarification", request)

    def parse_report_request(self, request):
        return self._next(self.report_requests, "parse_report_request", request)

    def write_narrative(self, request):
        if not self.narratives and "write_narrative" not in self.always_fail:
            # Narratives are optional: an unreachable model must never block a
            # report, so an empty queue behaves like a provider outage.
            self.calls.append("write_narrative")
            self.requests.append(("write_narrative", request))
            from kalampeach.ai.types import AIUnavailable

            raise AIUnavailable("narrative queue empty")
        return self._next(self.narratives, "write_narrative", request)


@pytest.fixture
def fake_ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def fake_telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def ctx(settings, db, fake_ai, fake_telegram, clock) -> AppContext:
    import logging

    return AppContext(
        settings=settings,
        db=db,
        ai=fake_ai,
        telegram=fake_telegram,
        clock=clock,
        logger=logging.getLogger("kalampeach.test"),
        worker_id="test-worker",
    )


# ---------------------------------------------------------------------------
# Object builders used across the suite
# ---------------------------------------------------------------------------


def extraction(**overrides) -> ExtractionResult:
    base = dict(
        classification="business_event",
        event_type="sale_revenue",
        confidence=0.9,
        event_date=None,
        amount=None,
        currency=None,
        item_name=None,
        quantity=None,
        unit=None,
        inventory_direction=None,
        description=None,
        missing_fields=(),
        ambiguities=(),
        reason=None,
    )
    base.update(overrides)
    return ExtractionResult(**base)


def non_business_extraction(classification="non_business") -> ExtractionResult:
    return extraction(classification=classification, event_type=None)


def correction(interpretation="correction", changes=None, reason=None, question=None) -> CorrectionResult:
    return CorrectionResult(
        interpretation=interpretation,
        changes=dict(changes or {}),
        reason=reason,
        clarification_question=question,
        confidence=0.8,
    )


def clarification_answer(values=None, missing_fields=(), reason=None) -> ClarificationAnswerResult:
    return ClarificationAnswerResult(
        values=dict(values or {}),
        missing_fields=tuple(missing_fields),
        ambiguities=(),
        reason=reason,
        confidence=0.8,
    )


def report_request(
    is_report_request=True,
    report_type="summary",
    period_kind="today",
    start_date=None,
    end_date=None,
    question=None,
) -> ReportRequestResult:
    return ReportRequestResult(
        is_report_request=is_report_request,
        report_type=report_type if is_report_request else None,
        period_kind=period_kind if is_report_request else None,
        start_date=start_date,
        end_date=end_date,
        clarification_question=question,
        confidence=0.9,
    )


def narrative(text="خلاصهٔ گزارش.") -> NarrativeResult:
    return NarrativeResult(narrative=text)


__all__ = [
    "BASE_ENV",
    "GROUP_ID",
    "MANAGER_ID",
    "EMPLOYEE_ID",
    "OTHER_EMPLOYEE_ID",
    "OUTSIDER_ID",
    "build_env",
    "MutableClock",
    "FakeAI",
    "FakeTelegram",
    "extraction",
    "non_business_extraction",
    "correction",
    "clarification_answer",
    "report_request",
    "narrative",
    "AIError",
    "AIInvalidOutput",
    "AIUnavailable",
]
