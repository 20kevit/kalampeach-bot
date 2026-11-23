"""Automatic capture of clear events and private clarification for ambiguous ones.

The decision rule is deterministic:

* ``non_business`` → ignore, no record, no group chatter;
* ``unclear`` → keep the source message and do not guess a category;
* ``business_event`` with every required field → save and acknowledge;
* ``business_event`` with a genuinely missing required field → save a pending
  candidate and ask the original author privately.

Re-running the same update is safe: the source message and the event both have
unique keys, and an undelivered question is re-sent instead of duplicated.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.ai.provider import ExtractionRequest
from app.ai.schema import Extraction
from app.context import AppContext
from app.domain import events as domain
from app.errors import DomainValidationError, TelegramForbidden, TelegramUnavailable
from app.logging_setup import get_logger
from app.messages_fa import (
    ACK_CONFIRMED,
    ack_line,
    clarification_question,
)
from app.repo import clarifications, events, messages, users
from app.services import outcome as outcomes
from app.services.authorization import Actor
from app.services.outcome import Outcome
from app.telegram.updates import NormalizedMessage
from app.timeutil import local_date, to_utc_iso

logger = get_logger("capture")


def extraction_to_raw(extraction: Extraction) -> dict[str, Any]:
    return {
        "event_type": extraction.event_type,
        "event_date": extraction.event_date,
        "amount": extraction.amount,
        "currency": extraction.currency,
        "item_name": extraction.item_name,
        "quantity": extraction.quantity,
        "unit": extraction.unit,
        "inventory_direction": extraction.inventory_direction,
        "description": extraction.description,
        "notes": None,
    }


def ordered_missing(fields: dict[str, Any], missing: list[str]) -> list[str]:
    """Stable field order for questions and audit rows."""

    event_type = fields.get("event_type")
    base = domain.REQUIRED_FIELDS.get(event_type, ("event_type",)) if event_type else ("event_type",)
    ordered = [field for field in base if field in missing]
    ordered.extend(sorted(field for field in missing if field not in ordered))
    return ordered


def record_source(
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    status: str,
    now_iso: str,
) -> sqlite3.Row:
    row, _created = messages.get_or_create(
        conn,
        telegram_update_id=message.update_id,
        chat_id=message.chat_id,
        message_id=message.message_id,
        reply_to_message_id=message.reply_to_message_id,
        sender_telegram_user_id=message.sender_id,
        chat_type=message.chat_type,
        message_text=message.text,
        message_timestamp_utc=to_utc_iso(message.sent_at),
        processing_status=status,
        now_iso=now_iso,
    )
    return row


def record_actor(ctx: AppContext, conn: sqlite3.Connection, actor: Actor, now_iso: str) -> None:
    users.record_observation(
        conn,
        telegram_user_id=actor.telegram_user_id,
        display_name=actor.display_name,
        role=actor.role,
        now_iso=now_iso,
    )


def handle_capture(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    actor: Actor,
    now: datetime,
) -> Outcome:
    now_iso = to_utc_iso(now)
    source = record_source(conn, message=message, status=messages.STATUS_RECEIVED, now_iso=now_iso)
    record_actor(ctx, conn, actor, now_iso)

    existing = events.get_by_source_message(conn, int(source["id"]))
    if existing is not None:
        return _resume_existing(ctx, conn, event=existing, actor=actor, now=now)

    local_day = local_date(message.sent_at, ctx.config.timezone)
    extraction = ctx.ai.extract_event(
        ExtractionRequest(
            text=message.text or "",
            chat_type=message.chat_type,
            message_local_date=local_day.isoformat(),
            message_local_datetime=local_datetime_text(message, ctx),
        )
    )

    if extraction.classification == "non_business":
        messages.set_status(conn, int(source["id"]), messages.STATUS_IGNORED)
        return outcomes.ignored("non_business")

    if extraction.classification == "unclear":
        # Business-ish but unclassifiable: keep provenance, do not invent a
        # category, and do not interrogate the employee about every message.
        messages.set_status(conn, int(source["id"]), messages.STATUS_UNCLEAR)
        return outcomes.ignored("unclear")

    try:
        fields, details = domain.normalise_fields(
            extraction_to_raw(extraction),
            message_local_date=local_day,
            default_currency=ctx.config.default_currency,
            allow_message_date_as_event_date=ctx.config.allow_message_date_as_event_date,
        )
    except DomainValidationError:
        # Value-level anomaly that schema validation did not catch. Fail closed:
        # no event, source preserved, operator-visible category.
        messages.set_status(conn, int(source["id"]), messages.STATUS_FAILED)
        return outcomes.failed("DOMAIN_VALIDATION")

    missing = domain.missing_required_fields(fields)
    # Only ask about fields that really are unknown; ignore model claims about
    # values it actually supplied.
    missing = [field for field in ordered_missing(fields, missing) if fields.get(field) is None]

    if missing:
        event_id = events.create(
            conn,
            fields=fields,
            details=details,
            status=domain.PENDING_CLARIFICATION,
            actor_telegram_user_id=actor.telegram_user_id,
            source_message_id=int(source["id"]),
            now_iso=now_iso,
        )
        messages.set_status(conn, int(source["id"]), messages.STATUS_PENDING)
        _ask(ctx, conn, event_id=event_id, employee_id=actor.telegram_user_id,
             fields=fields, missing=missing, round_number=1,
             prompt_source_message_id=int(source["id"]), now=now)
        return outcomes.done("pending_clarification")

    try:
        domain.validate_semantics(fields)
    except DomainValidationError:
        messages.set_status(conn, int(source["id"]), messages.STATUS_FAILED)
        return outcomes.failed("DOMAIN_VALIDATION")

    event_id = events.create(
        conn,
        fields=fields,
        details=details,
        status=domain.CONFIRMED,
        actor_telegram_user_id=actor.telegram_user_id,
        source_message_id=int(source["id"]),
        now_iso=now_iso,
    )
    messages.set_status(conn, int(source["id"]), messages.STATUS_PROCESSED)
    _acknowledge(ctx, message=message, fields=fields)
    logger.info(
        "event_confirmed",
        extra={"fields": {"event_id": event_id, "event_type": fields.get("event_type")}},
    )
    return outcomes.done("confirmed")


def _resume_existing(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    event: sqlite3.Row,
    actor: Actor,
    now: datetime,
) -> Outcome:
    """Idempotent re-entry for an update that was already captured."""

    if str(event["status"]) != domain.PENDING_CLARIFICATION:
        return outcomes.done("duplicate_event")

    open_row = clarifications.latest_open_for_event(conn, int(event["id"]))
    if open_row is None:
        fields = domain.fields_from_row(event)
        missing = [f for f in ordered_missing(fields, domain.missing_required_fields(fields)) if fields.get(f) is None]
        if not missing:
            missing = ["event_type"]
        _ask(
            ctx,
            conn,
            event_id=int(event["id"]),
            employee_id=actor.telegram_user_id,
            fields=fields,
            missing=missing,
            round_number=1,
            prompt_source_message_id=int(event["source_message_id"]),
            now=now,
        )
        return outcomes.done("pending_clarification")
    if open_row["prompt_message_id"] is None:
        _deliver_question(
            ctx,
            conn,
            clarification_id=int(open_row["id"]),
            employee_id=int(open_row["employee_telegram_user_id"]),
            question=str(open_row["question_text"]),
            now=now,
        )
    return outcomes.done("duplicate_pending")


def _ask(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    event_id: int,
    employee_id: int,
    fields: dict[str, Any],
    missing: list[str],
    round_number: int,
    prompt_source_message_id: int | None,
    now: datetime,
) -> int:
    question = clarification_question(fields, missing)
    clarification_id = clarifications.create(
        conn,
        event_id=event_id,
        employee_telegram_user_id=employee_id,
        question_text=question,
        missing_fields=missing,
        prompt_source_message_id=prompt_source_message_id,
        round_number=round_number,
        now_iso=to_utc_iso(now),
    )
    _deliver_question(
        ctx,
        conn,
        clarification_id=clarification_id,
        employee_id=employee_id,
        question=question,
        now=now,
    )
    return clarification_id


def _deliver_question(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    clarification_id: int,
    employee_id: int,
    question: str,
    now: datetime,
) -> None:
    """Send the private question; never fall back to posting in the group."""

    try:
        result = ctx.telegram.send_message(chat_id=employee_id, text=question)
    except TelegramForbidden:
        clarifications.mark_unable_to_deliver(
            conn, clarification_id, category="TELEGRAM_FORBIDDEN", now_iso=to_utc_iso(now)
        )
        logger.warning(
            "clarification_undeliverable",
            extra={"fields": {"clarification_id": clarification_id, "reason": "private chat not available"}},
        )
        return
    except TelegramUnavailable as exc:
        clarifications.mark_send_error(conn, clarification_id, category=exc.category)
        # The inbox retries this update; the pending clarification stays open and
        # the same question is re-sent rather than duplicated.
        raise
    clarifications.set_prompt_message(
        conn,
        clarification_id,
        chat_id=employee_id,
        message_id=int(result.message_id or 0),
    )


def _acknowledge(ctx: AppContext, *, message: NormalizedMessage, fields: dict[str, Any]) -> None:
    """Best-effort acknowledgement; never fails the saved event."""

    text = ack_line(ACK_CONFIRMED, fields)
    try:
        if message.is_private:
            ctx.telegram.send_message(chat_id=message.chat_id, text=text)
        elif ctx.config.send_group_acknowledgements:
            ctx.telegram.send_message(
                chat_id=message.chat_id, text=text, reply_to_message_id=message.message_id
            )
    except (TelegramForbidden, TelegramUnavailable) as exc:
        logger.info(
            "acknowledgement_failed",
            extra={"fields": {"category": getattr(exc, "category", "TELEGRAM_ERROR")}},
        )


def local_datetime_text(message: NormalizedMessage, ctx: AppContext) -> str:
    from app.timeutil import local_datetime

    return local_datetime(message.sent_at, ctx.config.timezone).isoformat(timespec="minutes")


def ask_for_clarification(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    event_id: int,
    employee_id: int,
    fields: dict[str, Any],
    missing: list[str],
    round_number: int,
    now: datetime,
) -> int:
    """Public entry point used by the clarification service for follow-ups."""

    return _ask(
        ctx,
        conn,
        event_id=event_id,
        employee_id=employee_id,
        fields=fields,
        missing=missing,
        round_number=round_number,
        prompt_source_message_id=None,
        now=now,
    )
