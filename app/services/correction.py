"""Reply-based corrections with append-only revisions.

The Telegram reply target identifies the event; the model only proposes a patch
for that event. It can never choose a different target, change status, or touch
history. Every accepted change appends a revision inside the same transaction
that updates the current projection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.context import AppContext
from app.domain import events as domain
from app.errors import DomainValidationError, TelegramForbidden, TelegramUnavailable
from app.logging_setup import get_logger
from app.messages_fa import (
    ACK_CORRECTED,
    CORRECTION_INVALID,
    CORRECTION_NO_CHANGE,
    CORRECTION_NOT_ALLOWED,
    FIELD_LABELS,
    ack_line,
)
from app.repo import clarifications, events, messages
from app.services import capture, clarification as clarification_service
from app.services import outcome as outcomes
from app.services.authorization import Actor, can_correct
from app.services.outcome import Outcome
from app.telegram.updates import NormalizedMessage
from app.timeutil import local_date, to_utc_iso

logger = get_logger("correction")


def handle_correction(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    actor: Actor,
    target_source: sqlite3.Row,
    event_row: sqlite3.Row,
    now: datetime,
) -> Outcome:
    event_id = int(event_row["id"])
    now_iso = to_utc_iso(now)

    if not can_correct(actor, int(event_row["created_by_telegram_user_id"])):
        if message.is_private:
            _notify(ctx, message.chat_id, CORRECTION_NOT_ALLOWED)
        logger.info(
            "correction_denied",
            extra={"fields": {"event_id": event_id, "actor": actor.telegram_user_id}},
        )
        return outcomes.ignored("correction_not_permitted")

    # A reply to the source message of an incomplete record is treated as
    # clarification, not as a revision of a confirmed event.
    if str(event_row["status"]) == domain.PENDING_CLARIFICATION:
        open_row = clarifications.latest_open_for_event(conn, event_id)
        if open_row is not None:
            return clarification_service.handle_answer(
                ctx,
                conn,
                message=message,
                actor=actor,
                clarification=open_row,
                now=now,
            )
        _notify(ctx, message.chat_id, CORRECTION_NO_CHANGE)
        return outcomes.ignored("pending_without_question")

    source = capture.record_source(
        conn, message=message, status=messages.STATUS_CORRECTION, now_iso=now_iso
    )
    source_id = int(source["id"])

    if events.has_revision_from_source(conn, event_id=event_id, source_message_id=source_id):
        return outcomes.done("duplicate_correction")

    current_fields = domain.fields_from_row(event_row)
    local_day = local_date(message.sent_at, ctx.config.timezone)

    interpretation = ctx.ai.interpret_correction(
        snapshot=domain.snapshot(current_fields, status=str(event_row["status"])),
        correction_text=message.text or "",
        message_local_date=local_day.isoformat(),
    )

    if not interpretation.patch:
        messages.set_status(conn, source_id, messages.STATUS_IGNORED)
        if message.is_private:
            _notify(ctx, message.chat_id, CORRECTION_NO_CHANGE)
        return outcomes.done("no_change")

    merged = domain.apply_patch(current_fields, interpretation.patch)
    try:
        fields, details = domain.normalise_fields(
            merged,
            message_local_date=local_day,
            default_currency=ctx.config.default_currency,
            allow_message_date_as_event_date=ctx.config.allow_message_date_as_event_date,
        )
        domain.validate_semantics(fields)
    except DomainValidationError as exc:
        messages.set_status(conn, source_id, messages.STATUS_FAILED)
        if message.is_private:
            labels = "، ".join(
                sorted({FIELD_LABELS.get(problem.split(" ")[0], "اطلاعات لازم") for problem in exc.problems})
            )
            _notify(ctx, message.chat_id, CORRECTION_INVALID.format(fields=labels))
        logger.info(
            "correction_invalid",
            extra={"fields": {"event_id": event_id, "problem_count": len(exc.problems)}},
        )
        return outcomes.done("invalid_correction")

    revision = events.apply_revision(
        conn,
        event_id=event_id,
        fields=fields,
        status=domain.CONFIRMED,
        details=details,
        actor_telegram_user_id=actor.telegram_user_id,
        source_message_id=source_id,
        reason=(interpretation.reason or "correction")[:500],
        now_iso=now_iso,
        validation_status="accepted",
    )
    messages.set_status(conn, source_id, messages.STATUS_PROCESSED)

    text = ack_line(ACK_CORRECTED, fields, revision_number=revision)
    if message.is_private:
        _notify(ctx, message.chat_id, text)
    elif ctx.config.send_group_acknowledgements:
        _notify(ctx, message.chat_id, text, reply_to_message_id=message.message_id)

    logger.info(
        "event_corrected",
        extra={"fields": {"event_id": event_id, "revision": revision}},
    )
    return outcomes.done("corrected")


def _notify(
    ctx: AppContext, chat_id: int, text: str, *, reply_to_message_id: int | None = None
) -> None:
    try:
        ctx.telegram.send_message(
            chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id
        )
    except TelegramForbidden:
        logger.info("correction_notice_undeliverable", extra={"fields": {"category": "TELEGRAM_FORBIDDEN"}})
    except TelegramUnavailable:
        logger.info("correction_notice_failed", extra={"fields": {"category": "TELEGRAM_UNAVAILABLE"}})
