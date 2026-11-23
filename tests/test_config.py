"""Configuration validation and fail-closed startup (TEST-PLAN G)."""

from __future__ import annotations

import pytest

from app.config import REQUIRED_CONFIG_KEYS, load_config, parse_env_file
from app.errors import ConfigError
from tests.helpers import BASE_ENV, env_for


def test_valid_configuration_loads(tmp_path):
    config = load_config(env_for(tmp_path))
    assert config.telegram_group_id == -1001234567890
    assert config.manager_telegram_id == 900001
    assert config.employee_telegram_ids == frozenset({900002, 900003})
    assert config.business_timezone == "Asia/Tehran"
    assert config.daily_report_time == "23:30"
    assert config.default_currency is None


def test_missing_required_keys_are_reported_without_values(tmp_path):
    env = env_for(tmp_path, TELEGRAM_BOT_TOKEN="", NVIDIA_API_KEY="")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env)
    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "NVIDIA_API_KEY" in message
    # No secret value ever appears in the error.
    assert "nvapi-TEST-NOT-REAL" not in message


def test_all_required_keys_are_covered_by_the_documented_list():
    assert "TELEGRAM_BOT_TOKEN" in REQUIRED_CONFIG_KEYS
    assert "NVIDIA_MODEL" in REQUIRED_CONFIG_KEYS
    assert "DATABASE_PATH" in REQUIRED_CONFIG_KEYS


@pytest.mark.parametrize("secret", ["short", "has space and commas", "x" * 300])
def test_weak_or_invalid_webhook_secret_is_rejected(tmp_path, secret):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env_for(tmp_path, TELEGRAM_WEBHOOK_SECRET=secret))
    assert "TELEGRAM_WEBHOOK_SECRET" in str(excinfo.value)


def test_webhook_url_must_be_https(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, WEBHOOK_PUBLIC_URL="http://bot.example.test/hook"))


def test_database_path_must_be_absolute_and_private(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, DATABASE_PATH="relative/db.sqlite3"))
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, DATABASE_PATH="/home/user/public_html/db.sqlite3"))


def test_log_file_must_not_be_public(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, LOG_FILE="/home/user/public_html/app.log"))


def test_business_timezone_is_fixed_by_specification(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, BUSINESS_TIMEZONE="Europe/Berlin"))
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, BUSINESS_TIMEZONE="Mars/Olympus"))


def test_daily_report_time_is_fixed_by_specification(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, DAILY_REPORT_TIME="21:00"))
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, DAILY_REPORT_TIME="25:99"))


def test_no_recorder_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env_for(tmp_path, EMPLOYEE_TELEGRAM_IDS="", MANAGER_CAN_RECORD_EVENTS="false")
        )
    assert "record" in str(excinfo.value).lower()


def test_manager_can_record_when_listed_or_enabled(tmp_path):
    listed = load_config(env_for(tmp_path, EMPLOYEE_TELEGRAM_IDS="900001"))
    assert listed.can_record(900001) is True

    enabled = load_config(
        env_for(tmp_path, EMPLOYEE_TELEGRAM_IDS="900002", MANAGER_CAN_RECORD_EVENTS="true")
    )
    assert enabled.can_record(900001) is True
    assert enabled.can_record(900099) is False

    disabled = load_config(env_for(tmp_path))
    assert disabled.can_record(900001) is False
    assert disabled.is_manager(900001) is True


def test_non_numeric_employee_ids_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, EMPLOYEE_TELEGRAM_IDS="900002,someone"))


def test_invalid_boolean_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, SEND_GROUP_ACKNOWLEDGEMENTS="maybe"))


def test_env_file_is_read_and_process_environment_wins(tmp_path):
    env_file = tmp_path / "private.env"
    env_file.write_text(
        "# comment\nexport MANAGER_TELEGRAM_ID='123456'\nNVIDIA_MODEL=from-file\n",
        encoding="utf-8",
    )
    environ = dict(BASE_ENV)
    environ.pop("MANAGER_TELEGRAM_ID")  # only present in the env file
    environ.update(
        {
            "DATABASE_PATH": str(tmp_path / "data" / "bot.sqlite3"),
            "KALAMPEACH_ENV_FILE": str(env_file),
            "NVIDIA_MODEL": "from-process",
        }
    )
    config = load_config(environ)
    assert config.manager_telegram_id == 123456
    assert config.nvidia_model == "from-process"


def test_missing_env_file_is_reported(tmp_path):
    environ = dict(BASE_ENV)
    environ["KALAMPEACH_ENV_FILE"] = str(tmp_path / "nope.env")
    with pytest.raises(ConfigError):
        load_config(environ)


def test_parse_env_file_keeps_dollar_signs_literal(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text('TELEGRAM_WEBHOOK_SECRET="abc$def_ghi-jkl-1234567890"\n', encoding="utf-8")
    values = parse_env_file(path)
    assert values["TELEGRAM_WEBHOOK_SECRET"] == "abc$def_ghi-jkl-1234567890"
