"""Authorization by numeric Telegram ID and configured chat ID.

Deny-by-default rules (SECURITY.md):

* group traffic is accepted only from the one configured group chat id;
* private traffic is accepted only from configured numeric user ids;
* the manager flag comes from configuration, never from a username or chat title;
* bot senders and unsupported chat types are rejected before any AI call.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..telegram.updates import TelegramMessage

OK = "ok"
BOT_SENDER = "bot_sender"
WRONG_GROUP = "wrong_group"
UNAUTHORIZED_USER = "unauthorized_user"
UNSUPPORTED_CHAT = "unsupported_chat"
NO_SENDER = "no_sender"


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    is_manager: bool
    can_record: bool
    chat_kind: str
    reason: str

    @property
    def is_private(self) -> bool:
        return self.chat_kind == "private"

    @property
    def is_group(self) -> bool:
        return self.chat_kind == "group"


def authorize(settings: Settings, message: TelegramMessage) -> AuthDecision:
    chat_kind = message.chat_type

    if message.sender_is_bot:
        return AuthDecision(False, False, False, chat_kind, BOT_SENDER)
    if message.sender_id is None:
        return AuthDecision(False, False, False, chat_kind, NO_SENDER)

    is_manager = settings.is_manager(message.sender_id)
    can_record = settings.can_record(message.sender_id)

    if chat_kind == "group":
        if int(message.chat_id) != int(settings.telegram_group_id):
            return AuthDecision(False, is_manager, can_record, chat_kind, WRONG_GROUP)
        if not (is_manager or can_record):
            return AuthDecision(False, is_manager, can_record, chat_kind, UNAUTHORIZED_USER)
        return AuthDecision(True, is_manager, can_record, chat_kind, OK)

    if chat_kind == "private":
        if not (is_manager or can_record):
            return AuthDecision(False, is_manager, can_record, chat_kind, UNAUTHORIZED_USER)
        return AuthDecision(True, is_manager, can_record, chat_kind, OK)

    return AuthDecision(False, is_manager, can_record, chat_kind, UNSUPPORTED_CHAT)
