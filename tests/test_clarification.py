"""Pending clarification and private delivery constraints (TEST-PLAN D)."""

from __future__ import annotations

from app.errors import TelegramForbidden
from app.services import clarification as clarification_service
from tests.helpers import (
    EMPLOYEE_ID,
    GROUP_ID,
    OTHER_EMPLOYEE_ID,
    build_context,
    group_update,
    process_once,
    private_update,
    sale_event,
)


def _event(ctx):
    with ctx.db.session() as conn:
        row = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def _clarifications(ctx):
    with ctx.db.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM clarifications ORDER BY id")]


def _revisions(ctx):
    with ctx.db.session() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM event_revisions ORDER BY id").fetchall()
        ]


def _start_pending(ctx, fake_ai, *, message_id=321):
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, group_update(1, "فروش انجام شد", message_id=message_id))
    event = _event(ctx)
    assert event["status"] == "pending_clarification"
    question = ctx.telegram.messages_to(EMPLOYEE_ID)[0]
    assert ctx.telegram.messages_to(GROUP_ID) == []  # no business detail in group
    return event, question


def test_ambiguous_group_event_asks_the_original_author_privately(ctx, fake_ai):
    _start_pending(ctx, fake_ai)
    row = _clarifications(ctx)[0]
    assert row["employee_telegram_user_id"] == EMPLOYEE_ID
    assert row["prompt_chat_id"] == EMPLOYEE_ID
    assert row["missing_fields_json"] == '["amount", "currency"]'
    assert row["status"] == "open"


def test_answer_by_reply_resolves_only_the_linked_clarification(ctx, fake_ai):
    event, question = _start_pending(ctx, fake_ai)
    fake_ai.queue_extraction(sale_event(amount="500000", currency="تومان"))
    process_once(
        ctx,
        private_update(2, "۵۰۰ هزار تومان", reply_to=question["message_id"]),
    )

    updated = _event(ctx)
    assert updated["status"] == "confirmed"
    assert updated["amount"] == "500000"
    assert updated["event_date"] == "2025-12-23"

    clarifications = _clarifications(ctx)
    assert len(clarifications) == 1
    assert clarifications[0]["status"] == "resolved"
    assert clarifications[0]["response_source_message_id"] is not None

    revisions = _revisions(ctx)
    assert [revision["revision_number"] for revision in revisions] == [1, 2]
    assert revisions[1]["reason"] == "clarification_answer"
    assert revisions[1]["actor_telegram_user_id"] == EMPLOYEE_ID
    assert "pending_clarification" in revisions[0]["new_snapshot_json"]

    # The employee gets a private confirmation; the group stays quiet.
    assert len(ctx.telegram.messages_to(EMPLOYEE_ID)) == 2
    assert ctx.telegram.messages_to(GROUP_ID) == []


def test_single_open_clarification_can_be_answered_without_a_reply(ctx, fake_ai):
    _start_pending(ctx, fake_ai)
    fake_ai.queue_extraction(sale_event(amount="12", currency="USD"))
    process_once(ctx, private_update(2, "۱۲ دلار"))
    assert _event(ctx)["status"] == "confirmed"


def test_incomplete_answer_triggers_exactly_one_follow_up_question(ctx, fake_ai):
    _start_pending(ctx, fake_ai)
    fake_ai.queue_extraction(sale_event(amount="500000", currency=None))
    process_once(ctx, private_update(2, "۵۰۰ هزار"))

    clarifications = _clarifications(ctx)
    assert len(clarifications) == 2
    assert clarifications[0]["status"] == "resolved"
    assert clarifications[1]["status"] == "open"
    assert clarifications[1]["round"] == 2
    assert "واحد پول" in clarifications[1]["question_text"]
    assert _event(ctx)["status"] == "pending_clarification"
    # Only questions, never a group post.
    assert all(message["chat_id"] != GROUP_ID for message in ctx.telegram.sent)


def test_unresolved_clarification_is_rejected_after_the_round_limit(ctx, fake_ai):
    _start_pending(ctx, fake_ai)
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, private_update(2, "نمی‌دانم"))
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, private_update(3, "باز هم نمی‌دانم"))
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, private_update(4, "واقعاً نمی‌دانم"))

    event = _event(ctx)
    assert event["status"] == "rejected"
    assert event["amount"] is None
    assert _clarifications(ctx)[-1]["status"] in {"resolved", "expired"}
    assert any("ثبت نشد" in message["text"] for message in ctx.telegram.messages_to(EMPLOYEE_ID))


def test_employee_who_never_started_the_bot_is_handled_without_exposing_details(tmp_path):
    ctx = build_context(tmp_path)
    ctx.telegram.queue_failure(TelegramForbidden("bot can't initiate conversation", cannot_initiate=True))
    ctx.ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, group_update(1, "فروش انجام شد"))

    clarifications = _clarifications(ctx)
    assert clarifications[0]["status"] == "unable_to_deliver"
    assert ctx.telegram.messages_to(GROUP_ID) == []
    assert _event(ctx)["status"] == "pending_clarification"


def test_two_open_clarifications_require_an_explicit_reply(ctx, fake_ai):
    _start_pending(ctx, fake_ai, message_id=321)
    fake_ai.queue_extraction(sale_event(amount=None, currency=None, description="دوم"))
    process_once(ctx, group_update(2, "فروش دوم انجام شد", message_id=322))

    assert len([row for row in _clarifications(ctx) if row["status"] == "open"]) == 2

    fake_ai.queue_extraction(sale_event(amount="99", currency="تومان"))
    process_once(ctx, private_update(3, "۹۹ تومان"))
    assert _event(ctx)["status"] == "pending_clarification"
    assert "همان پیام" in ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"]


def test_another_employee_cannot_answer_someone_elses_clarification(ctx, fake_ai):
    _start_pending(ctx, fake_ai)
    clarification = _clarifications(ctx)[0]
    from app.repo import clarifications
    from app.services.authorization import resolve_actor
    from app.telegram.updates import normalize_update

    actor = resolve_actor(ctx.config, OTHER_EMPLOYEE_ID)
    message = normalize_update(
        private_update(2, "تومان", sender_id=OTHER_EMPLOYEE_ID)
    ).message
    with ctx.db.session() as conn:
        row = clarifications.get(conn, clarification["id"])
        outcome = clarification_service.handle_answer(
            ctx, conn, message=message, actor=actor, clarification=row, now=ctx.now()
        )
    assert outcome.kind == "ignored"
    assert _event(ctx)["status"] == "pending_clarification"
    assert _clarifications(ctx)[0]["status"] == "open"


def test_answer_to_an_already_closed_question_creates_no_new_event(ctx, fake_ai):
    _event_row, question = _start_pending(ctx, fake_ai)
    fake_ai.queue_extraction(sale_event(amount="500000", currency="تومان"))
    process_once(ctx, private_update(2, "۵۰۰ هزار تومان", reply_to=question["message_id"]))
    assert _event(ctx)["status"] == "confirmed"

    process_once(ctx, private_update(3, "سلام", reply_to=question["message_id"]))
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert "قبلاً بسته" in ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"]
