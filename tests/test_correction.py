"""Reply-based corrections, permissions and append-only audit (TEST-PLAN E)."""

from __future__ import annotations

import json

from tests.helpers import (
    EMPLOYEE_ID,
    GROUP_ID,
    MANAGER_ID,
    OTHER_EMPLOYEE_ID,
    build_context,
    group_update,
    process_once,
    private_update,
    sale_event,
)

SOURCE_MESSAGE_ID = 555


def _capture_sale(ctx, fake_ai, *, amount="500000", currency="تومان", sender_id=EMPLOYEE_ID):
    fake_ai.queue_extraction(sale_event(amount=amount, currency=currency))
    process_once(
        ctx,
        group_update(1, "فروش ۵۰۰ هزار تومان", message_id=SOURCE_MESSAGE_ID, sender_id=sender_id),
    )
    with ctx.db.session() as conn:
        return dict(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())


def _capture_private_sale(ctx, fake_ai, *, amount="500000", currency="تومان", message_id=800):
    fake_ai.queue_extraction(sale_event(amount=amount, currency=currency))
    process_once(ctx, private_update(1, "فروش ۵۰۰ هزار تومان", message_id=message_id))
    with ctx.db.session() as conn:
        return dict(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())


def _revisions(ctx):
    with ctx.db.session() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM event_revisions ORDER BY id").fetchall()
        ]


def _event(ctx):
    with ctx.db.session() as conn:
        return dict(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())


def test_author_correction_appends_revision_and_updates_projection(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    fake_ai.queue_correction({"amount": "450000"}, reason="مبلغ اشتباه ثبت شده بود")
    process_once(
        ctx,
        group_update(2, "مبلغ ۴۵۰ هزار تومان بود", reply_to=SOURCE_MESSAGE_ID, message_id=900),
    )

    event = _event(ctx)
    assert event["amount"] == "450000"
    assert event["status"] == "confirmed"
    assert event["current_revision_number"] == 2

    revisions = _revisions(ctx)
    assert len(revisions) == 2
    assert revisions[0]["revision_number"] == 1
    assert revisions[0]["new_snapshot_json"]  # creation snapshot untouched
    previous = json.loads(revisions[1]["previous_snapshot_json"])
    new = json.loads(revisions[1]["new_snapshot_json"])
    assert previous["amount"] == "500000"
    assert new["amount"] == "450000"
    assert revisions[1]["reason"] == "مبلغ اشتباه ثبت شده بود"
    assert revisions[1]["actor_telegram_user_id"] == EMPLOYEE_ID
    assert "اصلاح ثبت شد" in ctx.telegram.messages_to(GROUP_ID)[-1]["text"]


def test_another_employee_cannot_correct_the_event(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    fake_ai.queue_correction({"amount": "1"})
    process_once(
        ctx,
        group_update(
            2,
            "مبلغ ۱ تومان بود",
            reply_to=SOURCE_MESSAGE_ID,
            sender_id=OTHER_EMPLOYEE_ID,
            message_id=901,
        ),
    )
    assert _event(ctx)["amount"] == "500000"
    assert len(_revisions(ctx)) == 1
    # Denial is silent in the group (no chatter, no leaked business detail) and
    # the AI patch is never applied.
    assert all("اجازه" not in message["text"] for message in ctx.telegram.messages_to(GROUP_ID))


def test_employee_can_correct_their_own_private_event(ctx, fake_ai):
    _capture_private_sale(ctx, fake_ai, message_id=811)
    fake_ai.queue_correction({"amount": "510000"})
    process_once(
        ctx,
        private_update(2, "مبلغ ۵۱۰ هزار تومان بود", reply_to=811, message_id=812),
    )
    assert _event(ctx)["amount"] == "510000"
    assert len(_revisions(ctx)) == 2
    assert "اصلاح ثبت شد" in ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"]


def test_manager_can_correct_their_own_private_event(tmp_path):
    ctx = build_context(tmp_path, MANAGER_CAN_RECORD_EVENTS="true")
    ctx.ai.queue_intent(intent="event", period_start=None, period_end=None)
    ctx.ai.queue_extraction(sale_event(amount="500000"))
    process_once(
        ctx,
        private_update(1, "فروش ۵۰۰ هزار تومان", sender_id=MANAGER_ID, message_id=830),
    )
    ctx.ai.queue_correction({"amount": "480000"})
    process_once(
        ctx,
        private_update(2, "مبلغ ۴۸۰ هزار تومان بود", sender_id=MANAGER_ID, reply_to=830, message_id=831),
    )
    assert _event(ctx)["amount"] == "480000"
    assert len(_revisions(ctx)) == 2


def test_manager_can_correct_in_the_group(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    fake_ai.queue_correction({"amount": "480000"})
    process_once(
        ctx,
        group_update(
            2,
            "اصلاح: ۴۸۰ هزار تومان",
            reply_to=SOURCE_MESSAGE_ID,
            sender_id=MANAGER_ID,
            message_id=902,
        ),
    )
    assert _event(ctx)["amount"] == "480000"
    assert len(_revisions(ctx)) == 2


def test_duplicate_correction_is_idempotent(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    fake_ai.queue_correction({"amount": "450000"})
    correction = group_update(2, "۴۵۰ هزار تومان", reply_to=SOURCE_MESSAGE_ID, message_id=903)
    process_once(ctx, correction)
    revision_count = len(_revisions(ctx))

    process_once(ctx, group_update(3, "۴۵۰ هزار تومان", reply_to=SOURCE_MESSAGE_ID, message_id=903))
    assert len(_revisions(ctx)) == revision_count
    assert _event(ctx)["current_revision_number"] == 2


def test_reply_to_a_non_event_message_is_not_turned_into_a_record(ctx, fake_ai):
    from tests.helpers import non_business

    fake_ai.queue_extraction(non_business())
    process_once(ctx, private_update(1, "سلام به همه", message_id=601))

    process_once(ctx, private_update(2, "اصلاحیه", reply_to=601))
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert "رویداد ثبت‌شده" in ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"]


def test_semantically_invalid_correction_is_refused(ctx, fake_ai):
    _capture_private_sale(ctx, fake_ai, message_id=821)
    # Re-labelling a sale as an inventory movement requires item, quantity,
    # unit and direction; the patch supplies none of them.
    fake_ai.queue_correction({"event_type": "inventory_movement"})
    process_once(
        ctx,
        private_update(2, "این یک گردش موجودی بود", reply_to=821, message_id=822),
    )
    event = _event(ctx)
    assert event["event_type"] == "sale_revenue"
    assert event["amount"] == "500000"
    assert len(_revisions(ctx)) == 1
    assert "اصلاح ثبت نشد" in ctx.telegram.messages_to(EMPLOYEE_ID)[-1]["text"]


def test_empty_correction_patch_makes_no_change(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    fake_ai.queue_correction({}, reason="هیچ تغییری نیست")
    process_once(ctx, group_update(2, "همان است", reply_to=SOURCE_MESSAGE_ID, message_id=904))
    assert len(_revisions(ctx)) == 1
    assert _event(ctx)["amount"] == "500000"


def test_history_remains_retrievable_after_multiple_corrections(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    for index, amount in enumerate(["450000", "400000"], start=2):
        fake_ai.queue_correction({"amount": amount})
        process_once(
            ctx,
            group_update(
                index, f"مبلغ {amount}", reply_to=SOURCE_MESSAGE_ID, message_id=910 + index
            ),
        )
    revisions = _revisions(ctx)
    assert [revision["revision_number"] for revision in revisions] == [1, 2, 3]
    assert json.loads(revisions[1]["previous_snapshot_json"])["amount"] == "500000"
    assert json.loads(revisions[2]["previous_snapshot_json"])["amount"] == "450000"
    assert json.loads(revisions[2]["new_snapshot_json"])["amount"] == "400000"


def test_correction_patch_cannot_include_unknown_fields(ctx, fake_ai):
    _capture_sale(ctx, fake_ai)
    from app.ai.schema import validate_correction
    from app.errors import AIInvalidOutput

    try:
        validate_correction({"patch": {"status": "confirmed"}, "reason": None})
    except AIInvalidOutput:
        pass
    else:  # pragma: no cover - safety net
        raise AssertionError("status must not be patchable")


def test_reply_to_a_pending_event_is_treated_as_clarification(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(amount=None, currency=None))
    process_once(ctx, group_update(1, "فروش انجام شد", message_id=SOURCE_MESSAGE_ID))
    fake_ai.queue_extraction(sale_event(amount="300000", currency="تومان"))
    process_once(
        ctx,
        group_update(2, "۳۰۰ هزار تومان", reply_to=SOURCE_MESSAGE_ID, message_id=920),
    )
    event = _event(ctx)
    assert event["status"] == "confirmed"
    assert event["amount"] == "300000"


def test_reply_resolves_the_exact_target_among_several_events(ctx, fake_ai):
    fake_ai.queue_extraction(sale_event(amount="100"))
    process_once(ctx, group_update(1, "فروش ۱۰۰ تومان", message_id=1101))
    fake_ai.queue_extraction(sale_event(amount="200", description="دوم"))
    process_once(ctx, group_update(2, "فروش ۲۰۰ تومان", message_id=1102))

    fake_ai.queue_correction({"amount": "150"})
    process_once(ctx, group_update(3, "۱۵۰ بود", reply_to=1101, message_id=1103))

    with ctx.db.session() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT e.amount, sm.message_id FROM events e "
                "JOIN source_messages sm ON sm.id = e.source_message_id ORDER BY e.id"
            )
        ]
    assert rows == [
        {"amount": "150", "message_id": 1101},
        {"amount": "200", "message_id": 1102},
    ]
