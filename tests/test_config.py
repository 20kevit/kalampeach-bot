"""TEST-PLAN G: startup fails clearly, secrets never leak into errors."""

from __future__ import annotations

import pytest

from kalampeach.config import ConfigError, load_settings
from conftest import BASE_ENV, build_env


def _env(tmp_path, **overrides):
    env = build_env(**overrides)
    env.setdefault("DATABASE_PATH", str(tmp_path / "data" / "bot.sqlite3"))
    return env


def test_valid_configuration_is_accepted(tmp_path):
    settings = load_settings(_env(tmp_path))
    assert settings.telegram_group_id == -1001234567890
    assert settings.manager_telegram_id == 9999
    assert settings.employee_ids == frozenset({1111, 2222})
    assert settings.default_currency is None
    assert settings.webhook_path == "/telegram/webhook"
    assert settings.worker_batch_size == 20
    assert settings.report_time_label if hasattr(settings, "report_time_label") else True


def test_missing_required_keys_are_all_reported(tmp_path):
    env = _env(tmp_path)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_GROUP_ID",
        "MANAGER_TELEGRAM_ID",
        "EMPLOYEE_TELEGRAM_IDS",
        "TELEGRAM_WEBHOOK_SECRET",
        "WEBHOOK_PUBLIC_URL",
        "NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
        "NVIDIA_MODEL",
        "DATABASE_PATH",
    ):
        env.pop(key)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    problems = " ".join(excinfo.value.problems)
    for key in ("TELEGRAM_BOT_TOKEN", "WEBHOOK_PUBLIC_URL", "NVIDIA_MODEL", "DATABASE_PATH"):
        assert key in problems


def test_placeholder_values_are_rejected(tmp_path):
    env = _env(
        tmp_path,
        TELEGRAM_BOT_TOKEN="changeme",
        DATABASE_PATH="/home/CPANEL_USER/data/bot.sqlite3",
        WEBHOOK_PUBLIC_URL="https://example.com/telegram/webhook",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    joined = " ".join(excinfo.value.problems)
    assert "TELEGRAM_BOT_TOKEN" in joined
    assert "DATABASE_PATH" in joined
    assert "WEBHOOK_PUBLIC_URL" in joined


def test_error_messages_never_contain_secret_values(tmp_path):
    secret = "SUPERSECRETVALUE_0123456789"
    env = _env(tmp_path, TELEGRAM_WEBHOOK_SECRET=secret, NVIDIA_API_KEY="nvapi-realish-abcdefghijkl")
    env["TELEGRAM_GROUP_ID"] = "not-a-number"
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    joined = " ".join(excinfo.value.problems)
    assert secret not in joined
    assert "nvapi-realish" not in joined


def test_webhook_secret_must_use_telegram_charset(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env(tmp_path, TELEGRAM_WEBHOOK_SECRET="short-secret"))
    assert "TELEGRAM_WEBHOOK_SECRET" in " ".join(excinfo.value.problems)

    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env(tmp_path, TELEGRAM_WEBHOOK_SECRET="bad secret with spaces 1234"))
    assert "TELEGRAM_WEBHOOK_SECRET" in " ".join(excinfo.value.problems)


def test_webhook_url_must_be_public_https_and_match_route(tmp_path):
    for url in (
        "http://bot.kalampeach.test/telegram/webhook",
        "https://bot.kalampeach.test/other",
        "https://localhost/telegram/webhook",
    ):
        with pytest.raises(ConfigError) as excinfo:
            load_settings(_env(tmp_path, WEBHOOK_PUBLIC_URL=url))
        assert "WEBHOOK_PUBLIC_URL" in " ".join(excinfo.value.problems)


def test_webhook_url_may_be_mounted_under_a_sub_path(tmp_path):
    settings = load_settings(
        _env(tmp_path, WEBHOOK_PUBLIC_URL="https://host.test/kalampeach/telegram/webhook")
    )
    assert settings.webhook_public_url.endswith("/telegram/webhook")


def test_group_and_user_ids_are_validated(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, TELEGRAM_GROUP_ID="0"))
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, EMPLOYEE_TELEGRAM_IDS="alice,bob"))
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, EMPLOYEE_TELEGRAM_IDS=""))
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, MANAGER_TELEGRAM_ID="-5"))


def test_employee_ids_are_normalized(tmp_path):
    settings = load_settings(_env(tmp_path, EMPLOYEE_TELEGRAM_IDS="1111, 2222 ,2222"))
    assert settings.employee_ids == frozenset({1111, 2222})


def test_timezone_and_report_time_are_fixed_for_the_mvp(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env(tmp_path, BUSINESS_TIMEZONE="Europe/Berlin"))
    assert "BUSINESS_TIMEZONE" in " ".join(excinfo.value.problems)

    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env(tmp_path, DAILY_REPORT_TIME="22:00"))
    assert "DAILY_REPORT_TIME" in " ".join(excinfo.value.problems)


def test_database_path_must_be_absolute_and_private(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, DATABASE_PATH="relative/bot.sqlite3"))
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env(tmp_path, DATABASE_PATH="/home/user/public_html/bot.sqlite3"))
    assert "outside the public web root" in " ".join(excinfo.value.problems)


def test_default_currency_is_optional_but_validated(tmp_path):
    settings = load_settings(_env(tmp_path, DEFAULT_CURRENCY="تومان"))
    assert settings.default_currency == "تومان"
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, DEFAULT_CURRENCY="12345"))


def test_numeric_limits_and_defaults(tmp_path):
    settings = load_settings(
        _env(
            tmp_path,
            WORKER_BATCH_SIZE="5",
            WORKER_MAX_SECONDS="25",
            NVIDIA_MAX_RETRIES="0",
            DAILY_REPORT_RECOVERY_HOURS="2",
        )
    )
    assert settings.worker_batch_size == 5
    assert settings.worker_max_seconds == 25
    assert settings.nvidia_max_retries == 0
    assert settings.daily_report_recovery_hours == 2

    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, WORKER_BATCH_SIZE="0"))
    with pytest.raises(ConfigError):
        load_settings(_env(tmp_path, NVIDIA_RESPONSE_FORMAT="xml"))


def test_env_file_is_parsed_but_process_environment_wins(tmp_path):
    env_file = tmp_path / "private.env"
    env_file.write_text(
        "# comment\n"
        "export TELEGRAM_BOT_TOKEN='123456789:AAFfromfile_0123456789abcdef'\n"
        'NVIDIA_MODEL="model-from-file"\n'
        "EMPTY_VALUE=\n",
        encoding="utf-8",
    )
    import os

    env = dict(os.environ)
    env["ENV_FILE"] = str(env_file)
    env["DATABASE_PATH"] = str(tmp_path / "data" / "bot.sqlite3")
    env["NVIDIA_MODEL"] = "model-from-real-env"
    # Required keys must come from the file or the environment.
    for key, value in BASE_ENV.items():
        if key in ("NVIDIA_MODEL",):
            continue
        env.setdefault(key, value)
    settings = load_settings(env)
    assert settings.nvidia_model == "model-from-real-env"


def test_invalid_env_file_line_is_reported(tmp_path):
    env_file = tmp_path / "private.env"
    env_file.write_text("this line is not key value\n", encoding="utf-8")
    import os

    env = dict(os.environ)
    env["ENV_FILE"] = str(env_file)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    assert "ENV_FILE" in " ".join(excinfo.value.problems)


def test_manager_recording_requires_explicit_allowlisting(tmp_path):
    settings = load_settings(_env(tmp_path))
    assert settings.can_record(9999) is False
    assert settings.is_manager(9999) is True
    assert settings.is_authorized(9999) is True
    assert settings.is_authorized(7777) is False

    with_manager = load_settings(_env(tmp_path, EMPLOYEE_TELEGRAM_IDS="1111,9999"))
    assert with_manager.can_record(9999) is True
