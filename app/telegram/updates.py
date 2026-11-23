"""Normalization of Telegram updates into application-owned structures.

Only plain text messages are business input. Everything else — edits, channel
posts, callbacks, media, service messages, bot senders — is classified here so
the processor can ignore it without calling the AI provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.errors import AppError

MEDIA_KEYS = (
    "photo",
    "voice",
    "audio",
    "video",
    "document",
    "sticker",
    "animation",
    "video_note",
    "location",
    "venue",
    "contact",
    "poll",
    "dice",
    "invoice",
    "successful_payment",
    "story",
)

SERVICE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "pinned_message",
    "forum_topic_created",
    "forum_topic_closed",
    "forum_topic_reopened",
    "forum_topic_edited",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_scheduled",
    "video_chat_participants_invited",
    "web_app_data",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "proximity_alert_triggered",
    "write_access_allowed",
)

KIND_MESSAGE = "message"
KIND_EDITED = "edited_message"
KIND_UNSUPPORTED = "unsupported_update"


class UpdateFormatError(AppError):
    """The webhook payload is not a usable Telegram update."""

    category = "UPDATE_FORMAT"


@dataclass(frozen=True)
class NormalizedMessage:
    update_id: int
    chat_id: int
    chat_type: str
    message_id: int
    sender_id: int | None
    sender_is_bot: bool
    sender_display_name: str | None
    text: str | None
    sent_at: datetime
    reply_to_message_id: int | None
    reply_to_sender_id: int | None
    reply_to_is_bot: bool
    media_kind: str | None
    is_service: bool

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


@dataclass(frozen=True)
class NormalizedUpdate:
    update_id: int
    kind: str
    message: NormalizedMessage | None
    reason: str | None = None


def extract_update_id(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        raise UpdateFormatError("update payload must be a JSON object")
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise UpdateFormatError("update_id must be an integer")
    return update_id


def normalize_update(payload: Mapping[str, Any]) -> NormalizedUpdate:
    update_id = extract_update_id(payload)

    if "edited_message" in payload or "edited_channel_post" in payload:
        return NormalizedUpdate(update_id, KIND_EDITED, None, "edited messages are not processed")

    raw = payload.get("message")
    if not isinstance(raw, Mapping):
        return NormalizedUpdate(
            update_id, KIND_UNSUPPORTED, None, "update type is not a new message"
        )

    message = normalize_message(update_id, raw)
    return NormalizedUpdate(update_id, KIND_MESSAGE, message)


def normalize_message(update_id: int, raw: Mapping[str, Any]) -> NormalizedMessage:
    chat = raw.get("chat")
    if not isinstance(chat, Mapping):
        raise UpdateFormatError("message has no chat object")
    chat_id = chat.get("id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise UpdateFormatError("message chat id must be an integer")
    chat_type = str(chat.get("type") or "unknown")

    message_id = raw.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise UpdateFormatError("message_id must be an integer")

    sender = raw.get("from") if isinstance(raw.get("from"), Mapping) else {}
    sender_id = sender.get("id") if isinstance(sender.get("id"), int) else None
    sender_is_bot = bool(sender.get("is_bot", False))
    display_parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
    ]
    display_name = " ".join(part for part in display_parts if part) or None

    reply_to = raw.get("reply_to_message")
    reply_to_message_id = None
    reply_to_sender_id = None
    reply_to_is_bot = False
    if isinstance(reply_to, Mapping):
        candidate = reply_to.get("message_id")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            reply_to_message_id = candidate
        reply_from = reply_to.get("from")
        if isinstance(reply_from, Mapping):
            if isinstance(reply_from.get("id"), int):
                reply_to_sender_id = int(reply_from["id"])
            reply_to_is_bot = bool(reply_from.get("is_bot", False))

    text = raw.get("text")
    if not isinstance(text, str):
        text = None

    media_kind = next((key for key in MEDIA_KEYS if key in raw), None)
    is_service = any(key in raw for key in SERVICE_KEYS)

    sent_at = datetime.now(tz=timezone.utc)
    unix_date = raw.get("date")
    if isinstance(unix_date, int) and not isinstance(unix_date, bool):
        try:
            sent_at = datetime.fromtimestamp(unix_date, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            sent_at = datetime.now(tz=timezone.utc)

    return NormalizedMessage(
        update_id=update_id,
        chat_id=int(chat_id),
        chat_type=chat_type,
        message_id=int(message_id),
        sender_id=int(sender_id) if isinstance(sender_id, int) else None,
        sender_is_bot=sender_is_bot,
        sender_display_name=display_name,
        text=text,
        sent_at=sent_at,
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id=reply_to_sender_id,
        reply_to_is_bot=reply_to_is_bot,
        media_kind=media_kind,
        is_service=is_service,
    )
