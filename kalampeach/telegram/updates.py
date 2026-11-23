"""Normalization of Telegram updates into the application's own shape.

Only plain text messages are supported by the MVP. Everything else is either
ignored (bot/service/unsupported update types) or flagged as unsupported media
so the worker can send a short private notice instead of pretending to read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from ..timeutil import parse_unix_seconds

SUPPORTED_UPDATE_KEYS = ("message",)

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
    "contact",
    "poll",
    "dice",
    "venue",
)

MAX_TEXT_LENGTH = 4096


class UpdatePayloadError(ValueError):
    """Raised when an update cannot be normalized (permanent failure)."""


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    chat_type: str  # normalized: group | private | channel | other
    telegram_chat_type: str  # raw value from Telegram
    message_id: int
    reply_to_message_id: Optional[int]
    sender_id: Optional[int]
    sender_is_bot: bool
    sender_display_name: Optional[str]
    text: Optional[str]
    has_unsupported_media: bool
    message_timestamp_utc: datetime

    @property
    def has_text(self) -> bool:
        return self.text is not None and bool(self.text.strip())


def _normalize_chat_type(raw: object) -> str:
    value = str(raw or "").lower()
    if value in ("group", "supergroup"):
        return "group"
    if value == "private":
        return "private"
    if value == "channel":
        return "channel"
    return "other"


def _display_name(sender: Mapping[str, Any]) -> Optional[str]:
    parts = [sender.get("first_name"), sender.get("last_name")]
    name = " ".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())
    if name:
        return name[:120]
    username = sender.get("username")
    if isinstance(username, str) and username.strip():
        return ("@" + username.strip())[:120]
    return None


def normalize_update(payload: Mapping[str, Any]) -> Optional[TelegramMessage]:
    """Convert a raw Telegram update, or return ``None`` for unsupported types."""

    if not isinstance(payload, Mapping):
        raise UpdatePayloadError("update payload must be an object")

    update_id = payload.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        raise UpdatePayloadError("update_id is missing or not an integer")

    message = payload.get("message")
    if not isinstance(message, Mapping):
        # edited_message, callback_query, channel_post, inline_query, ...
        return None

    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        raise UpdatePayloadError("message has no chat")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise UpdatePayloadError("message chat id is invalid")

    message_id = message.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise UpdatePayloadError("message_id is invalid")

    sent_at = parse_unix_seconds(message.get("date"))
    if sent_at is None:
        raise UpdatePayloadError("message date is invalid")

    reply_to = message.get("reply_to_message")
    reply_to_id: Optional[int] = None
    if isinstance(reply_to, Mapping):
        candidate = reply_to.get("message_id")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            reply_to_id = candidate
    if reply_to_id is None:
        # Reply threads in groups may expose the root via external_reply.
        external = message.get("external_reply")
        if isinstance(external, Mapping):
            candidate = external.get("message_id")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                reply_to_id = candidate

    sender = message.get("from")
    sender_id: Optional[int] = None
    sender_is_bot = False
    sender_name: Optional[str] = None
    if isinstance(sender, Mapping):
        candidate = sender.get("id")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            sender_id = candidate
        sender_is_bot = bool(sender.get("is_bot"))
        sender_name = _display_name(sender)

    raw_text = message.get("text")
    text: Optional[str] = None
    if isinstance(raw_text, str):
        stripped = raw_text.strip()
        text = stripped[:MAX_TEXT_LENGTH] if stripped else None
    elif "caption" in message and isinstance(message.get("caption"), str):
        # Captions accompany unsupported media; never treat them as extraction input.
        text = None

    has_media = any(key in message for key in MEDIA_KEYS)

    return TelegramMessage(
        update_id=update_id,
        chat_id=chat_id,
        chat_type=_normalize_chat_type(chat.get("type")),
        telegram_chat_type=str(chat.get("type") or ""),
        message_id=message_id,
        reply_to_message_id=reply_to_id,
        sender_id=sender_id,
        sender_is_bot=sender_is_bot,
        sender_display_name=sender_name,
        text=text,
        has_unsupported_media=has_media,
        message_timestamp_utc=sent_at,
    )


def extract_chat_hint(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Cheap chat metadata for the inbox row (no message content)."""

    message = payload.get("message") if isinstance(payload, Mapping) else None
    if not isinstance(message, Mapping):
        return {"chat_id": None, "chat_type": None}
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return {"chat_id": None, "chat_type": None}
    chat_id = chat.get("id")
    return {
        "chat_id": chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None,
        "chat_type": _normalize_chat_type(chat.get("type")),
    }
