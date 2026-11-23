"""Private clarification lifecycle.

Linking rules (deterministic, no guessing):

1. a reply to the bot's question message resolves that clarification;
2. otherwise, if the employee has exactly one open clarification, a private
   message is treated as its answer;
3. otherwise the employee is asked to reply to the question message.

A question is never posted in the group. If the bot cannot open a private chat
(the employee never pressed Start), the clarification is marked
``unable_to_deliver`` and only logged — no business detail is revealed publicly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.ai.provider import ClarificationContext, ExtractionRequest
from app.context import AppContext
from app.domain import events as domain
from app.errors import DomainValidationError, TelegramForbidden, TelegramUnavailable
from app.logging_setup import get_logger
from app.messages_fa import (
    CLARIFICATION_ALREADY_CLOSED,
    CLARIFICATION_EXHAUSTED,
    clarification_answered,
)
from app.repo import clarifications, events, messages
from app.services import capture
from app.services import outcome as outcomes
from app.services.authorization import Actor
from app.services.outcome import Outcome
from app.telegram.updates import NormalizedMessage
from app.timeutil import local_date, to_utc_iso

logger = get_logger("clarification")


def handle_answer(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    actor: Actor,
    clarification: sqlite3.Row,
    now: datetime,
) -> Outcome:
    now_iso = to_utc_iso(now)
    source = capture.record_source(
        conn, message=message, status=messages.STATUS_CLARIFICATION, now_iso=now_iso
    )
    source_id = int(source["id"])
    clarification_id = int(clarification["id"])
    event_id = int(clarification["event_id"])

    if int(clarification["employee_telegram_user_id"]) != actor.telegram_user_id:
        # Only the employee who was asked may answer; no detail is revealed.
        return outcomes.ignored("clarification_not_addressed_to_sender")

    status = str(clarification["status"])
    if status == clarifications.STATUS_OPEN:
        clarifications.mark_answered(
            conn, clarification_id, response_source_message_id=source_id
        )
    elif status == clarifications.STATUS_ANSWERED:
        if int(clarification["response_source_message_id"] or 0) != source_id:
            return outcomes.ignored("stale_clarification")
    else:
        _notify(ctx, actor.telegram_user_id, CLARIFICATION_ALREADY_CLOSED)
        return outcomes.ignored("clarification_closed")

    event = events.get(conn, event_id)
    if event is None:
        return outcomes.failed("EVENT_MISSING")

    if events.has_revision_from_source(conn, event_id=event_id, source_message_id=source_id):
        return outcomes.done("duplicate_answer")

    if str(event["status"]) != domain.PENDING_CLARIFICATION:
        clarifications.mark_resolved(conn, clarification_id, now_iso=now_iso)
        _notify(ctx, actor.telegram_user_id, CLARIFICATION_ALREADY_CLOSED)
        return outcomes.ignored("event_not_pending")

    current_fields = domain.fields_from_row(event)
    original_source = messages.get(conn, int(event["source_message_id"]))
    original_text = str(original_source["message_text"] or "") if original_source else ""
    local_day = local_date(message.sent_at, ctx.config.timezone)

    extraction = ctx.ai.extract_event(
        ExtractionRequest(
            text=message.text or "",
            chat_type=message.chat_type,
            message_local_date=local_day.isoformat(),
            message_local_datetime=capture.local_datetime_text(message, ctx),
            clarification=ClarificationContext(
                missing_fields=tuple(clarifications.missing_fields(clarification)),
                question_text=str(clarification["question_text"]),
                answer_text=message.text or "",
                original_text=original_text,
            ),
            current_snapshot=current_fields,
        )
    )

    merged_raw = dict(current_fields)
    if extraction.classification == "business_event":
        for key, value in capture.extraction_to_raw(extraction).items():
            if value is not None:
                merged_raw[key] = value

    try:
        fields, details = domain.normalise_fields(
            merged_raw,
            message_local_date=local_day,
            default_currency=ctx.config.default_currency,
            allow_message_date_as_event_date=ctx.config.allow_message_date_as_event_date,
        )
    except DomainValidationError:
        messages.set_status(conn, source_id, messages.STATUS_FAILED)
        return outcomes.failed("DOMAIN_VALIDATION")

    missing = [f for f in domain.missing_required_fields(fields) if fields.get(f) is None]

    if not missing:
        try:
            domain.validate_semantics(fields)
        except DomainValidationError:
            messages.set_status(conn, source_id, messages.STATUS_FAILED)
            return outcomes.failed("DOMAIN_VALIDATION")
        revision = events.apply_revision(
            conn,
            event_id=event_id,
            fields=fields,
            status=domain.CONFIRMED,
            details=details,
            actor_telegram_user_id=actor.telegram_user_id,
            source_message_id=source_id,
            reason="clarification_answer",
            now_iso=now_iso,
            clarification_id=clarification_id,
        )
        clarifications.mark_resolved(conn, clarification_id, now_iso=now_iso)
        messages.set_status(conn, source_id, messages.STATUS_PROCESSED)
        messages.set_status(conn, int(event["source_message_id"]), messages.STATUS_PROCESSED)
        _notify(ctx, actor.telegram_user_id, clarification_answered(fields))
        logger.info(
            "event_clarified",
            extra={"fields": {"event_id": event_id, "revision": revision}},
        )
        return outcomes.done("clarified")

    round_number = int(clarification["round"]) + 1
    if round_number > ctx.config.max_clarification_rounds:
        events.apply_revision(
            conn,
            event_id=event_id,
            fields=fields,
            status=domain.REJECTED,
            details={"rejected_reason": "clarification_exhausted"},
            actor_telegram_user_id=actor.telegram_user_id,
            source_message_id=source_id,
            reason="clarification_exhausted",
            now_iso=now_iso,
            clarification_id=clarification_id,
        )
        clarifications.mark_resolved(conn, clarification_id, now_iso=now_iso)
        clarifications.close_open_for_event(
            conn, event_id, status=clarifications.STATUS_EXPIRED, now_iso=now_iso
        )
        messages.set_status(conn, source_id, messages.STATUS_IGNORED)
        messages.set_status(conn, int(event["source_message_id"]), messages.STATUS_FAILED)
        _notify(ctx, actor.telegram_user_id, CLARIFICATION_EXHAUSTED)
        logger.info(
            "event_rejected_after_clarification",
            extra={"fields": {"event_id": event_id, "rounds": round_number - 1}},
        )
        return outcomes.done("rejected")

    # Close this round before opening the next one, so a retried update cannot
    # create a second follow-up question.
    clarifications.mark_resolved(conn, clarification_id, now_iso=now_iso)
    ordered = capture.ordered_missing(fields, missing)
    capture.ask_for_clarification(
        ctx,
        conn,
        event_id=event_id,
        employee_id=actor.telegram_user_id,
        fields=fields,
        missing=ordered,
        round_number=round_number,
        now=now,
    )
    return outcomes.done("pending_clarification")


def _notify(ctx: AppContext, chat_id: int, text: str) -> None:
    """Private notice; delivery problems never fail an already-settled update."""

    try:
        ctx.telegram.send_message(chat_id=chat_id, text=text)
    except TelegramForbidden:
        logger.info("private_notice_undeliverable", extra={"fields": {"category": "TELEGRAM_FORBIDDEN"}})
    except TelegramUnavailable:
        logger.info("private_notice_failed", extra={"fields": {"category": "TELEGRAM_UNAVAILABLE"}})
