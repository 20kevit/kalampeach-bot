"""Best-effort outbound notices with bot-message provenance.

Acknowledgements and setup hints are *best effort*: a failure is logged with a
safe category and never blocks or corrupts the business write. Anything that must
be durably delivered (clarification questions, reports) is handled by its own
retry-aware code path instead of this helper.
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import repository
from ..telegram.client import TelegramError
from ..timeutil import to_utc_iso
from .context import AppContext

logger = logging.getLogger("kalampeach.notify")


class Notifier:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def send(
        self,
        chat_id: int,
        text: str,
        *,
        kind: str = "notice",
        event_id: Optional[int] = None,
        clarification_id: Optional[int] = None,
        report_request_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[int]:
        if not text:
            return None
        try:
            message_id = self.ctx.telegram.send_message(
                chat_id, text, reply_to_message_id=reply_to_message_id
            )
        except TelegramError as exc:
            logger.warning(
                "notice_failed kind=%s chat_id=%s category=%s",
                kind,
                chat_id,
                exc.category,
            )
            return None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("notice_failed kind=%s error=%s", kind, exc.__class__.__name__)
            return None

        try:
            with self.ctx.db.transaction() as connection:
                repository.record_bot_message(
                    connection,
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    kind=kind,
                    event_id=event_id,
                    clarification_id=clarification_id,
                    report_request_id=report_request_id,
                    now=to_utc_iso(self.ctx.now()),
                )
        except Exception as exc:  # pragma: no cover - provenance is best effort
            logger.warning("bot_message_record_failed error=%s", exc.__class__.__name__)
        return int(message_id)
