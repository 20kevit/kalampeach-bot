"""TEST-PLAN A: authorization, group isolation, manager-only reports."""

from __future__ import annotations

import pytest

from kalampeach.services import messages
from conftest import (
    EMPLOYEE_ID,
    GROUP_ID,
    MANAGER_ID,
    OUTSIDER_ID,
    extraction,
    non_business_extraction,
    report_request,
    build_env,
)
from helpers import (
    events,
    fetch_all,
    manager_update,
    private_update,
    process,
    report_deliveries,
    run_worker,
    telegram_update,
)


def test_authorized_employee_group_message_is_processed(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="1000", currency="تومان"))
    payload = telegram_update(update_id=1, message_id=10, text="فروش ۱۰۰۰ تومان")
    process(ctx, payload, skip_reports=True)

    rows = events(ctx)
    assert len(rows) == 1
    assert rows[0]["created_by_telegram_user_id"] == EMPLOYEE_ID
    assert fake_ai.calls == ["extract_event"]


def test_authorized_employee_private_message_is_processed(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(extraction(amount="2000", currency="تومان"))
    payload = private_update(update_id=1, message_id=10, text="فروش ۲۰۰۰ تومان")
    process(ctx, payload, skip_reports=True)

    assert len(events(ctx)) == 1
    # A private confirmation is sent; the group never receives one.
    assert fake_telegram.texts_to(EMPLOYEE_ID)
    assert fake_telegram.texts_to(GROUP_ID) == []


def test_unauthorized_user_cannot_create_events(ctx, fake_ai, fake_telegram):
    payload = private_update(
        update_id=1, message_id=10, text="فروش ۹۹۹ تومان", sender_id=OUTSIDER_ID, chat_id=OUTSIDER_ID
    )
    process(ctx, payload, skip_reports=True)

    assert events(ctx) == []
    assert fake_ai.calls == [], "unauthorized content must never reach the AI provider"
    assert fake_telegram.texts_to(OUTSIDER_ID) == [messages.ACCESS_DENIED]


def test_message_from_unconfigured_group_is_ignored(ctx, fake_ai, fake_telegram):
    payload = telegram_update(
        update_id=1, message_id=10, text="فروش ۵۰۰ تومان", chat_id=-1009999999999
    )
    process(ctx, payload, skip_reports=True)

    assert events(ctx) == []
    assert fake_ai.calls == []
    assert fake_telegram.sent == []


def test_employees_cannot_request_management_reports(ctx, fake_ai, fake_telegram):
    fake_ai.extractions.append(non_business_extraction())
    payload = private_update(update_id=1, message_id=10, text="گزارش فروش امروز را بده")
    process(ctx, payload, skip_reports=True)

    assert report_deliveries(ctx) == []
    assert fetch_all(ctx, "SELECT * FROM report_requests") == []
    assert fake_ai.calls == ["extract_event"], "only the manager may trigger report parsing"
    assert fake_telegram.texts_to(EMPLOYEE_ID) == []


def test_authorization_uses_numeric_ids_not_usernames(ctx, fake_ai):
    # Same numeric id, different display name and username: still authorized.
    fake_ai.extractions.append(extraction(amount="100", currency="تومان"))
    first = telegram_update(update_id=1, message_id=10, text="فروش", first_name="علی")
    first["message"]["from"]["username"] = "ali_new_name"
    process(ctx, first, skip_reports=True)
    assert len(events(ctx)) == 1

    # A stranger whose username looks like the manager is still denied.
    impostor = telegram_update(
        update_id=2,
        message_id=11,
        text="فروش ۲۰۰ تومان",
        sender_id=OUTSIDER_ID,
        chat_type="private",
        chat_id=OUTSIDER_ID,
        first_name="مدیر",
    )
    impostor["message"]["from"]["username"] = "manager_official"
    process(ctx, impostor, skip_reports=True)
    assert len(events(ctx)) == 1
    assert fake_ai.calls == ["extract_event"]


def test_bot_senders_are_never_processed(ctx, fake_ai):
    payload = telegram_update(update_id=1, message_id=10, text="فروش", is_bot=True)
    process(ctx, payload, skip_reports=True)
    assert events(ctx) == []
    assert fake_ai.calls == []


def test_unsupported_update_types_are_ignored(ctx, fake_ai):
    payload = {"update_id": 1, "callback_query": {"id": "abc", "from": {"id": EMPLOYEE_ID}}}
    process(ctx, payload, skip_reports=True)
    assert events(ctx) == []
    assert fake_ai.calls == []
    with ctx.db.connection() as connection:
        row = connection.execute("SELECT status FROM telegram_updates WHERE update_id = 1").fetchone()
    assert row["status"] == "done"


def test_telegram_service_message_is_ignored(ctx, fake_ai):
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1_800_000_000,
            "chat": {"id": GROUP_ID, "type": "supergroup"},
            "new_chat_members": [{"id": EMPLOYEE_ID, "is_bot": False, "first_name": "x"}],
        },
    }
    process(ctx, payload, skip_reports=True)
    assert events(ctx) == []
    assert fake_ai.calls == []


def test_manager_records_only_when_allowlisted(ctx, fake_ai, fake_telegram):
    # MANAGER_ID is not in EMPLOYEE_TELEGRAM_IDS in the default test config.
    fake_ai.report_requests.append(report_request(is_report_request=False, period_kind=None))
    payload = manager_update(update_id=1, message_id=10, text="فروش ۱۰۰۰ تومان")
    process(ctx, payload, skip_reports=True)

    assert events(ctx) == []
    assert fake_ai.calls == ["parse_report_request"]
    assert messages.MANAGER_CANNOT_RECORD in fake_telegram.texts_to(MANAGER_ID)


def test_manager_in_allowlist_can_record(ctx, fake_ai, settings, db, clock, fake_telegram):
    from dataclasses import replace

    from kalampeach.services.context import AppContext

    settings = replace(settings, employee_ids=frozenset({EMPLOYEE_ID, MANAGER_ID}))
    ctx = AppContext(
        settings=settings, db=db, ai=fake_ai, telegram=fake_telegram, clock=clock, worker_id="w2"
    )
    fake_ai.report_requests.append(report_request(is_report_request=False, period_kind=None))
    fake_ai.extractions.append(extraction(amount="700", currency="تومان"))
    payload = manager_update(update_id=1, message_id=10, text="فروش ۷۰۰ تومان")
    process(ctx, payload, skip_reports=True)

    rows = events(ctx)
    assert len(rows) == 1
    assert rows[0]["created_by_telegram_user_id"] == MANAGER_ID


def test_manager_can_read_while_employee_media_is_not_extracted(ctx, fake_ai, fake_telegram):
    payload = private_update(update_id=1, message_id=10, text="", media="voice")
    process(ctx, payload, skip_reports=True)
    assert fake_ai.calls == []
    assert fake_telegram.texts_to(EMPLOYEE_ID) == [messages.UNSUPPORTED_MEDIA]
    assert events(ctx) == []


def test_group_media_does_not_trigger_chatter(ctx, fake_ai, fake_telegram):
    payload = telegram_update(update_id=1, message_id=10, text=None, media="photo")
    process(ctx, payload, skip_reports=True)
    assert fake_ai.calls == []
    assert fake_telegram.sent == []
    assert events(ctx) == []


def test_users_table_records_roles(ctx, fake_ai):
    fake_ai.extractions.append(extraction(amount="10", currency="تومان"))
    process(ctx, telegram_update(update_id=1, message_id=10, text="فروش"), skip_reports=True)
    with ctx.db.connection() as connection:
        row = connection.execute(
            "SELECT role, display_name FROM users WHERE telegram_user_id = ?", (EMPLOYEE_ID,)
        ).fetchone()
    assert row["role"] == "employee"
    assert row["display_name"] == "کارمند"


def test_denied_updates_are_marked_done_without_side_effects(ctx, fake_ai):
    payload = telegram_update(
        update_id=1, message_id=10, text="فروش", chat_id=-1009999999999
    )
    process(ctx, payload, skip_reports=True)
    with ctx.db.connection() as connection:
        row = connection.execute(
            "SELECT status FROM telegram_updates WHERE update_id = 1"
        ).fetchone()
    assert row["status"] == "done"
    assert fetch_all(ctx, "SELECT * FROM source_messages") == []
