"""Update routing: authorization first, AI second, storage last.

Routing summary
---------------
group message:
    configured group + authorized sender + plain text → capture, or a correction
    when the message replies to a stored event's source message. Everything else
    is ignored without any AI call and without group chatter.

private message:
    authorized sender only; replies to a question resolve that clarification,
    replies to an event's source message are corrections, a manager's report
    requests are handled by the reporting service, and anything else is a new
    event candidate.

Media and service messages are never sent to the AI provider in the MVP.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.context import AppContext
from app.repo import clarifications, events, messages
from app.services import capture, clarification, correction, reporting
from app.services import outcome as outcomes
from app.services.authorization import Actor, is_configured_group, resolve_actor
from app.services.outcome import Outcome
from app.telegram.updates import (
    KIND_MESSAGE,
    NormalizedMessage,
    NormalizedUpdate,
    UpdateFormatError,
    normalize_update,
)
from app.logging_setup import get_logger
from app.messages_fa import (
    ACCESS_DENIED,
    CLARIFICATION_MULTIPLE_OPEN,
    RECORDING_DISABLED,
    REPLY_TARGET_NOT_AN_EVENT,
    UNSUPPORTED_MEDIA,
    WELCOME_EMPLOYEE,
    WELCOME_MANAGER,
)

logger = get_logger("processor")


def process_inbox_row(
    ctx: AppContext, conn: sqlite3.Connection, row: sqlite3.Row, *, now: datetime
) -> Outcome:
    try:
        payload = json.loads(str(row["payload_json"]))
    except ValueError:
        return outcomes.failed("UPDATE_PAYLOAD_INVALID")

    try:
        update = normalize_update(payload)
    except UpdateFormatError:
        return outcomes.failed("UPDATE_FORMAT")

    if update.kind != KIND_MESSAGE or update.message is None:
        return outcomes.ignored(update.reason or "unsupported_update")

    message = update.message
    if message.sender_is_bot or message.is_service:
        return outcomes.ignored("bot_or_service_message")

    if message.is_private:
        return _handle_private(ctx, conn, message=message, update=update, now=now)
    return _handle_group(ctx, conn, message=message, now=now)


# ------------------------------------------------------------------------ group
def _handle_group(
    ctx: AppContext, conn: sqlite3.Connection, *, message: NormalizedMessage, now: datetime
) -> Outcome:
    config = ctx.config
    if not is_configured_group(config, message.chat_id):
        return outcomes.ignored("unconfigured_chat")

    actor = resolve_actor(config, message.sender_id, message.sender_display_name)
    if actor is None:
        # Unauthorized content never reaches the AI provider.
        return outcomes.ignored("unauthorized_sender")

    if message.media_kind:
        # No group chatter for unsupported media; private chats get a notice.
        return outcomes.ignored("unsupported_media")

    if not message.has_text:
        return outcomes.ignored("no_text")

    if message.reply_to_message_id is not None:
        reply_outcome = _handle_reply_reference(
            ctx, conn, message=message, actor=actor, now=now
        )
        if reply_outcome is not None:
            return reply_outcome

    return capture.handle_capture(ctx, conn, message=message, actor=actor, now=now)


# ---------------------------------------------------------------------- private
def _handle_private(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    update: NormalizedUpdate,
    now: datetime,
) -> Outcome:
    actor = resolve_actor(ctx.config, message.sender_id, message.sender_display_name)
    text = (message.text or "").strip()

    if actor is None:
        if text.startswith("/start"):
            _safe_send(ctx, message.chat_id, ACCESS_DENIED)
        logger.info(
            "unauthorized_private_message",
            extra={"fields": {"update_kind": update.kind}},
        )
        return outcomes.ignored("unauthorized_sender")

    if text.split("@")[0] in {"/start", "/help"}:
        _safe_send(ctx, message.chat_id, WELCOME_MANAGER if actor.is_manager else WELCOME_EMPLOYEE)
        return outcomes.done("command")

    if message.media_kind:
        _safe_send(ctx, message.chat_id, UNSUPPORTED_MEDIA)
        return outcomes.ignored("unsupported_media")

    if not message.has_text:
        return outcomes.ignored("no_text")

    if message.reply_to_message_id is not None:
        reply_outcome = _handle_reply_reference(
            ctx, conn, message=message, actor=actor, now=now
        )
        if reply_outcome is not None:
            return reply_outcome

    if actor.is_manager:
        report_outcome = reporting.try_handle_manager_message(
            ctx, conn, message=message, actor=actor, now=now
        )
        if report_outcome is not None:
            return report_outcome

    open_rows = clarifications.open_for_employee(conn, actor.telegram_user_id)
    if len(open_rows) == 1:
        return clarification.handle_answer(
            ctx, conn, message=message, actor=actor, clarification=open_rows[0], now=now
        )
    if len(open_rows) > 1:
        _safe_send(ctx, message.chat_id, CLARIFICATION_MULTIPLE_OPEN)
        return outcomes.ignored("multiple_open_clarifications")

    if not actor.can_record:
        _safe_send(ctx, message.chat_id, RECORDING_DISABLED)
        return outcomes.ignored("recording_disabled")

    return capture.handle_capture(ctx, conn, message=message, actor=actor, now=now)


# ------------------------------------------------------------------ reply logic
def _handle_reply_reference(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    actor: Actor,
    now: datetime,
) -> Outcome | None:
    """Resolve a reply to a clarification question or to an event source message.

    Returns ``None`` when the reply target is unrelated, so the caller can treat
    the message as a new event candidate.
    """

    reply_to = message.reply_to_message_id
    if reply_to is None:
        return None

    prompt = clarifications.find_by_prompt_message(
        conn, chat_id=message.chat_id, message_id=reply_to
    )
    if prompt is not None:
        return clarification.handle_answer(
            ctx, conn, message=message, actor=actor, clarification=prompt, now=now
        )

    target_source = messages.get_by_telegram_ref(
        conn, chat_id=message.chat_id, message_id=reply_to
    )
    if target_source is None:
        return None

    event_row = events.get_by_source_message(conn, int(target_source["id"]))
    if event_row is None:
        if message.is_private:
            _safe_send(ctx, message.chat_id, REPLY_TARGET_NOT_AN_EVENT)
        return outcomes.ignored("reply_to_non_event")

    return correction.handle_correction(
        ctx,
        conn,
        message=message,
        actor=actor,
        target_source=target_source,
        event_row=event_row,
        now=now,
    )


def _safe_send(ctx: AppContext, chat_id: int, text: str) -> None:
    from app.errors import TelegramForbidden, TelegramUnavailable

    try:
        ctx.telegram.send_message(chat_id=chat_id, text=text)
    except (TelegramForbidden, TelegramUnavailable):
        logger.info("notice_failed", extra={"fields": {"category": "TELEGRAM"}})
