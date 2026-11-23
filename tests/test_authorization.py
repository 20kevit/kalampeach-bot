"""Authorization, group isolation and routing gates (TEST-PLAN A)."""

from __future__ import annotations

from app.messages_fa import ACCESS_DENIED, RECORDING_DISABLED, UNSUPPORTED_MEDIA
from tests.helpers import (
    EMPLOYEE_ID,
    GROUP_ID,
    MANAGER_ID,
    OTHER_EMPLOYEE_ID,
    OUTSIDER_ID,
    group_update,
    non_business,
    process_once,
    private_update,
    sale_event,
    telegram_message,
)


def _events(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events").fetchall()]


def _inbox_statuses(ctx):
    with ctx.db.session() as conn:
        return [row["status"] for row in conn.execute("SELECT status FROM telegram_inbox")]


def test_authorized_group_message_is_processed(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۱,۵۰۰,۰۰۰ تومان"))
    events = _events(ctx)
    assert len(events) == 1
    assert events[0]["event_type"] == "sale_revenue"
    assert events[0]["created_by_telegram_user_id"] == EMPLOYEE_ID
    assert len(fake_ai.extraction_requests) == 1


def test_authorized_private_message_is_processed(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(ctx, private_update(1, "فروش ۵۰۰ هزار تومان"))
    assert len(_events(ctx)) == 1
    assert ctx.telegram.messages_to(EMPLOYEE_ID), "expected a private acknowledgement"
    assert ctx.telegram.messages_to(GROUP_ID) == []


def test_unauthorized_user_cannot_create_events_and_never_reaches_ai(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۹۰۰ هزار تومان", sender_id=OUTSIDER_ID))
    process_once(ctx, private_update(2, "فروش ۹۰۰ هزار تومان", sender_id=OUTSIDER_ID))
    assert _events(ctx) == []
    assert fake_ai.extraction_requests == []
    assert _inbox_statuses(ctx) == ["ignored", "ignored"]


def test_unauthorized_private_start_gets_generic_denial(ctx):
    process_once(ctx, private_update(1, "/start", sender_id=OUTSIDER_ID))
    sent = ctx.telegram.messages_to(OUTSIDER_ID)
    assert len(sent) == 1
    assert sent[0]["text"] == ACCESS_DENIED


def test_message_from_unconfigured_group_is_ignored(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(
        ctx,
        telegram_message(
            update_id=1, text="فروش ۱ میلیون تومان", chat_id=-1009999999999, chat_type="supergroup"
        ),
    )
    assert _events(ctx) == []
    assert fake_ai.extraction_requests == []
    assert ctx.telegram.sent == []


def test_ordinary_employee_cannot_get_management_report(ctx, fake_ai):
    fake_ai.queue_extraction(non_business())
    process_once(ctx, private_update(1, "گزارش فروش امروز"))
    # The employee's text goes through event capture, never the report path.
    assert fake_ai.intent_requests == []
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0] == 0
    assert all("گزارش" not in m["text"] or "📊" not in m["text"] for m in ctx.telegram.sent)


def test_numeric_id_authorization_survives_username_changes(ctx, fake_ai):
    first = telegram_message(update_id=1, text="فروش ۱۰۰ تومان")
    first["message"]["from"]["first_name"] = "Old Name"
    first["message"]["from"]["username"] = "old_username"
    second = telegram_message(update_id=2, text="فروش ۲۰۰ تومان", message_id=2222)
    second["message"]["from"]["first_name"] = "New Name"
    second["message"]["from"]["username"] = "new_username"

    fake_ai.queue_extraction(sale_event(amount="100"))
    process_once(ctx, first)
    fake_ai.queue_extraction(sale_event(amount="200"))
    process_once(ctx, second)

    assert len(_events(ctx)) == 2
    with ctx.db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (EMPLOYEE_ID,)).fetchone()
    assert row["display_name"] == "New Name"


def test_bot_and_service_messages_never_become_events(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    bot_message = telegram_message(update_id=1, text="فروش ۱۰۰ تومان", is_bot=True)
    service_message = telegram_message(
        update_id=2, text=None, new_chat_members=[{"id": 5, "is_bot": False, "first_name": "A"}]
    )
    process_once(ctx, bot_message)
    process_once(ctx, service_message)
    assert _events(ctx) == []
    assert fake_ai.extraction_requests == []


def test_manager_records_only_when_enabled(tmp_path):
    from tests.helpers import build_context

    ctx = build_context(tmp_path, MANAGER_CAN_RECORD_EVENTS="false")
    ctx.ai.queue_intent(intent="event", period_start=None, period_end=None)
    process_once(ctx, private_update(1, "فروش ۵۰۰ هزار تومان", sender_id=MANAGER_ID))
    assert ctx.telegram.messages_to(MANAGER_ID)[-1]["text"] == RECORDING_DISABLED
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    ctx2 = build_context(
        tmp_path / "second", MANAGER_CAN_RECORD_EVENTS="true"
    )
    ctx2.ai.queue_intent(intent="event", period_start=None, period_end=None)
    ctx2.ai.queue_extraction(sale_event())
    process_once(ctx2, private_update(1, "فروش ۵۰۰ هزار تومان", sender_id=MANAGER_ID))
    with ctx2.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_other_employee_can_record_but_not_read_reports(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۱۰۰ تومان", sender_id=OTHER_EMPLOYEE_ID))
    assert len(_events(ctx)) == 1
    assert fake_ai.intent_requests == []


def test_group_media_is_ignored_without_chatter(ctx, fake_ai):
    process_once(ctx, telegram_message(update_id=1, text=None, photo=[{"file_id": "x"}]))
    assert fake_ai.extraction_requests == []
    assert ctx.telegram.sent == []


def test_private_media_gets_a_short_unsupported_notice(ctx):
    update = private_update(1, None, photo=[{"file_id": "x"}])
    process_once(ctx, update)
    assert ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"] == UNSUPPORTED_MEDIA


def test_group_start_command_does_not_create_business_noise(ctx, fake_ai):
    fake_ai.queue_extraction(non_business())
    process_once(ctx, group_update(1, "/start"))
    assert ctx.telegram.sent == []
    assert _events(ctx) == []
