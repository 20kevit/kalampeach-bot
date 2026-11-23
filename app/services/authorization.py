"""Authorization by numeric Telegram identity.

Configuration is the source of truth: a numeric Telegram user ID plus the one
configured group chat ID. Usernames, display names, group titles, membership and
message content are never used to grant access. Everything is deny-by-default,
and unauthorized senders are filtered before any AI call.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Config

ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"


@dataclass(frozen=True)
class Actor:
    telegram_user_id: int
    role: str
    can_record: bool
    is_manager: bool
    display_name: str | None = None

    @property
    def can_request_reports(self) -> bool:
        return self.is_manager


def resolve_actor(
    config: Config, telegram_user_id: int | None, display_name: str | None = None
) -> Actor | None:
    """Return the actor for an authorized numeric ID, else ``None``."""

    if telegram_user_id is None:
        return None
    is_manager = config.is_manager(telegram_user_id)
    is_employee = telegram_user_id in config.employee_telegram_ids
    if not is_manager and not is_employee:
        return None
    return Actor(
        telegram_user_id=telegram_user_id,
        role=ROLE_MANAGER if is_manager else ROLE_EMPLOYEE,
        can_record=config.can_record(telegram_user_id),
        is_manager=is_manager,
        display_name=display_name,
    )


def is_configured_group(config: Config, chat_id: int) -> bool:
    return chat_id == config.telegram_group_id


def can_correct(actor: Actor, event_created_by: int) -> bool:
    """An employee may correct their own event; the manager may correct any."""

    return actor.is_manager or actor.telegram_user_id == event_created_by
