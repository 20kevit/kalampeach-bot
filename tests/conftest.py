"""Shared fixtures. Every test runs offline with fake AI/Telegram and a temp DB."""

from __future__ import annotations

import pytest

from app.wsgi import create_app
from tests.helpers import (
    FIXED_NOW,
    FakeAI,
    FakeTelegram,
    build_context,
    make_config,
)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def fake_ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def fake_telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def ctx(tmp_path, fake_ai, fake_telegram):
    return build_context(tmp_path, ai=fake_ai, telegram=fake_telegram)


@pytest.fixture
def client(ctx):
    app = create_app(ctx)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def fixed_now():
    return FIXED_NOW
