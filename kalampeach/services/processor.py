"""Routing of one durably-queued Telegram update.

Order of decisions (each one denies by default):

1. normalize the update; unsupported update types are ignored;
2. authorize the numeric sender id and the configured chat id;
3. persist the source message idempotently;
4. plain text only — media gets a short private notice, never fake extraction;
5. reply targets (clarification prompt / confirmation / source message) are
   resolved deterministically from stored Telegram message ids;
6. otherwise: manager private chat -> report intent, everyone else -> extraction.

No AI call is ever made for unauthorized senders or for bot/service updates.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

from .. import repository
from ..ai.types import (
    AIConfigurationError,
    AIError,
    AIInvalidOutput,
    ExtractionRequest,
    ReportRequestRequest,
)
from ..telegram.updates import UpdatePayloadError, normalize_update
from ..timeutil import to_utc_iso
from . import auth, messages
from .capture import CaptureService, clarification_accepts_answer
from .context import AppContext
from .corrections import CorrectionService
from .errors import (
    AI_INVALID_OUTPUT,
    AI_UNAVAILABLE,
    INVALID_PAYLOAD,
    PermanentProcessingError,
    RetryableProcessingError,
)
from .notify import Notifier
from .outcomes import ServiceOutcome
from .reports import ReportService

logger = logging.getLogger("kalampeach.processor")


class MessageProcessor:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.notifier = Notifier(ctx)
        self.capture = CaptureService(ctx, self.notifier)
        self.corrections = CorrectionService(ctx, self.notifier)
        self.reports = ReportService(ctx, self.notifier)

    # ------------------------------------------------------------------
    def process(self, update_row) -> ServiceOutcome:
        try:
            payload = json.loads(update_row["raw_payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PermanentProcessingError(INVALID_PAYLOAD) from exc

        try:
            message = normalize_update(payload)
        except UpdatePayloadError as exc:
            raise PermanentProcessingError(INVALID_PAYLOAD) from exc

        if message is None:
            return ServiceOutcome(action="ignored", note="unsupported_update_type")

        decision = auth.authorize(self.ctx.settings, message)
        if not decision.allowed:
            if decision.is_private and decision.reason == auth.UNAUTHORIZED_USER:
                self.notifier.send(message.chat_id, messages.ACCESS_DENIED, kind="notice")
            logger.info(
                "update_denied update_id=%s reason=%s chat_type=%s",
                message.update_id,
                decision.reason,
                message.chat_type,
            )
            return ServiceOutcome(action="ignored", note=decision.reason)

        source, created = self._persist_source(message)
        source_id = int(source["id"])
        if not created and source["processing_status"] != "received":
            return ServiceOutcome(action="duplicate", note="source_already_processed")

        if not message.has_text:
            if message.has_unsupported_media and message.chat_type == "private":
                self.notifier.send(message.chat_id, messages.UNSUPPORTED_MEDIA, kind="notice")
            self._mark_source(source_id, "ignored", "unsupported_input")
            return ServiceOutcome(
                action="ignored",
                note="unsupported_media" if message.has_unsupported_media else "no_text",
            )

        self._upsert_user(message)

        try:
            return self._route(message=message, source_id=source_id, decision=decision)
        except AIInvalidOutput as exc:
            self._mark_source(source_id, "failed", AI_INVALID_OUTPUT)
            logger.error("ai_invalid_output update_id=%s", message.update_id)
            raise PermanentProcessingError(AI_INVALID_OUTPUT, source_message_id=source_id) from exc
        except AIConfigurationError as exc:
            self._mark_source(source_id, "failed", AI_UNAVAILABLE)
            raise PermanentProcessingError(AI_UNAVAILABLE, source_message_id=source_id) from exc
        except AIError as exc:
            logger.warning("ai_unavailable update_id=%s category=%s", message.update_id, exc.category)
            raise RetryableProcessingError(AI_UNAVAILABLE, source_message_id=source_id) from exc

    # ------------------------------------------------------------------
    def _route(self, *, message, source_id: int, decision) -> ServiceOutcome:
        if message.reply_to_message_id is not None:
            target_kind, target = self._resolve_reply_target(message)
            handled = self._handle_reply_target(
                message=message,
                source_id=source_id,
                target_kind=target_kind,
                target=target,
                decision=decision,
            )
            if handled is not None:
                return handled

        if decision.is_manager and decision.is_private:
            return self._handle_manager_private(message=message, source_id=source_id, decision=decision)

        if not decision.can_record:
            self._mark_source(source_id, "ignored", "not_a_recorder")
            if decision.is_private:
                self.notifier.send(message.chat_id, messages.MANAGER_CANNOT_RECORD, kind="notice")
            return ServiceOutcome(action="ignored", note="not_a_recorder")

        return self._extract_and_capture(message=message, source_id=source_id, decision=decision)

    # ------------------------------------------------------------------
    def _handle_reply_target(
        self, *, message, source_id: int, target_kind: str, target, decision
    ) -> Optional[ServiceOutcome]:
        if target_kind == "clarification":
            if int(target["employee_telegram_user_id"]) != int(message.sender_id or 0):
                self._mark_source(source_id, "ignored", "clarification_not_owner")
                return ServiceOutcome(action="ignored", note="clarification_not_owner")
            return self.capture.handle_answer(
                message=message, source_message_id=source_id, clarification=target
            )

        if target_kind == "report_request":
            if not decision.is_manager:
                self._mark_source(source_id, "ignored", "report_request_not_manager")
                return ServiceOutcome(action="ignored", note="not_manager")
            parsed = self._parse_report_request(
                text=message.text or "", prior_request=target["request_text"]
            )
            return self.reports.handle_period_answer(
                message=message,
                source_message_id=source_id,
                request_row=target,
                parsed=parsed,
            )

        if target_kind == "event":
            event = target
            if str(event["status"]) == "pending_clarification":
                pending = self._awaiting_clarification_for(event, message)
                if pending is not None:
                    return self.capture.handle_answer(
                        message=message, source_message_id=source_id, clarification=pending
                    )
            return self.corrections.handle(
                message=message, source_message_id=source_id, event=event
            )

        if target_kind in ("ambiguous", "unresolved"):
            # Always answer privately: never explain reply targets in the group.
            self._mark_source(source_id, "ignored", "%s_reply_target" % target_kind)
            if message.sender_id is not None:
                self.notifier.send(
                    int(message.sender_id), messages.REPLY_TARGET_UNRESOLVED, kind="notice"
                )
            return ServiceOutcome(action="ignored", note="%s_reply_target" % target_kind)

        return None

    def _resolve_reply_target(self, message) -> Tuple[str, object]:
        reply_to = int(message.reply_to_message_id)
        chat_id = int(message.chat_id)

        with self.ctx.db.connection() as connection:
            bot_message = repository.find_bot_message(connection, chat_id, reply_to)
            if bot_message is not None:
                kind = str(bot_message["kind"])
                if kind == "clarification_prompt" and bot_message["clarification_id"]:
                    clarification = repository.get_clarification(
                        connection, int(bot_message["clarification_id"])
                    )
                    if clarification is not None:
                        return "clarification", clarification
                if kind == "report_prompt" and bot_message["report_request_id"]:
                    request_row = repository.get_report_request(
                        connection, int(bot_message["report_request_id"])
                    )
                    if request_row is not None:
                        return "report_request", request_row
                if kind == "event_confirmation" and bot_message["event_id"]:
                    event = repository.get_event(connection, int(bot_message["event_id"]))
                    if event is not None:
                        return "event", event
                return "unresolved", None

            source = repository.get_source_message_by_telegram(connection, chat_id, reply_to)
            if source is None:
                # The quoted message is unknown to us. That is exactly what a reply
                # to a clarification question looks like in the window between
                # Telegram accepting the send and the local mapping being committed
                # (or forever, if the sending worker died before recording it).
                # A question in ``sending``/``delivery_uncertain`` is durable proof
                # that such a message may exist, so attribute the reply to it when
                # there is exactly one such question for this sender.
                # Questions are only ever sent to an employee's private chat, so
                # only a private reply from that same numeric id may be attributed;
                # a group reply is left unresolved rather than guessed.
                private_reply = (
                    message.chat_type == "private"
                    and int(message.chat_id) == int(message.sender_id or 0)
                )
                unresolved = (
                    repository.find_unconfirmed_clarifications(
                        connection, employee_telegram_user_id=int(message.sender_id or 0)
                    )
                    if private_reply
                    else []
                )
                if len(unresolved) == 1:
                    logger.info(
                        "reply_attributed_to_unconfirmed_clarification clarification_id=%s"
                        " update_id=%s",
                        int(unresolved[0]["id"]),
                        message.update_id,
                    )
                    return "clarification", unresolved[0]
                if len(unresolved) > 1:
                    logger.warning(
                        "reply_target_ambiguous_unconfirmed_clarifications count=%d update_id=%s",
                        len(unresolved),
                        message.update_id,
                    )
                return "unresolved", None

            candidates = []
            event = repository.find_event_by_source_message(connection, int(source["id"]))
            if event is not None:
                candidates.append(event)
            candidates.extend(
                repository.find_events_by_revision_source(connection, int(source["id"]))
            )
            unique = {int(row["id"]): row for row in candidates}
            if len(unique) == 1:
                return "event", next(iter(unique.values()))
            if len(unique) > 1:
                return "ambiguous", None
            return "unresolved", None

    def _awaiting_clarification_for(self, event, message):
        now_iso = self.ctx.now_iso()
        with self.ctx.db.connection() as connection:
            rows = repository.list_open_clarifications(connection, int(message.sender_id or 0))
        candidates = [
            row
            for row in rows
            if int(row["event_id"]) == int(event["id"])
            and clarification_accepts_answer(row, now_iso)
        ]
        if not candidates:
            return None
        # Prefer a question the employee definitely received, then the newest.
        candidates.sort(key=lambda row: (row["status"] != "awaiting_answer", -int(row["id"])))
        return candidates[0]

    # ------------------------------------------------------------------
    def _handle_manager_private(self, *, message, source_id: int, decision) -> ServiceOutcome:
        manager_id = int(self.ctx.settings.manager_telegram_id)
        with self.ctx.db.connection() as connection:
            open_request = repository.find_open_report_request(connection, manager_id)

        if open_request is not None:
            parsed = self._parse_report_request(
                text=message.text or "", prior_request=open_request["request_text"]
            )
            if parsed.is_report_request or parsed.period_kind not in (None, "ambiguous"):
                return self.reports.handle_period_answer(
                    message=message,
                    source_message_id=source_id,
                    request_row=open_request,
                    parsed=parsed,
                )

        parsed = self._parse_report_request(text=message.text or "")
        if parsed.is_report_request:
            return self.reports.handle_manager_request(
                message=message, source_message_id=source_id, parsed=parsed
            )

        if not decision.can_record:
            self._mark_source(source_id, "ignored", "not_a_recorder")
            self.notifier.send(message.chat_id, messages.MANAGER_CANNOT_RECORD, kind="notice")
            return ServiceOutcome(action="ignored", note="not_a_recorder")

        return self._extract_and_capture(message=message, source_id=source_id, decision=decision)

    def _parse_report_request(self, *, text: str, prior_request: Optional[str] = None):
        return self.ctx.ai.parse_report_request(
            ReportRequestRequest(
                text=text,
                today_local=self.ctx.today_local(),
                prior_request=prior_request,
            )
        )

    def _extract_and_capture(self, *, message, source_id: int, decision) -> ServiceOutcome:
        extraction = self.ctx.ai.extract_event(
            ExtractionRequest(
                text=message.text or "",
                chat_type=message.chat_type,
                sender_role="manager" if decision.is_manager else "employee",
                message_date_local=self.ctx.local_date_of(message.message_timestamp_utc),
            )
        )
        return self.capture.handle_new_message(
            message=message, source_message_id=source_id, extraction=extraction
        )

    # ------------------------------------------------------------------
    def _persist_source(self, message):
        now_iso = self.ctx.now_iso()
        with self.ctx.db.transaction() as connection:
            row, created = repository.record_source_message(
                connection,
                telegram_update_id=message.update_id,
                chat_id=int(message.chat_id),
                chat_type=message.chat_type,
                message_id=int(message.message_id),
                reply_to_message_id=message.reply_to_message_id,
                sender_telegram_user_id=message.sender_id,
                message_text=message.text or "",
                message_timestamp_utc=to_utc_iso(message.message_timestamp_utc),
                processing_status="received",
                now=now_iso,
            )
        return row, created

    def _upsert_user(self, message) -> None:
        if message.sender_id is None:
            return
        role = "manager" if self.ctx.settings.is_manager(message.sender_id) else "employee"
        with self.ctx.db.transaction() as connection:
            repository.upsert_user(
                connection,
                telegram_user_id=int(message.sender_id),
                display_name=message.sender_display_name,
                role=role,
                is_active=True,
                now=self.ctx.now_iso(),
            )

    def _mark_source(self, source_id: int, status: str, reason: Optional[str]) -> None:
        try:
            with self.ctx.db.transaction() as connection:
                repository.set_source_status(
                    connection,
                    source_message_id=source_id,
                    processing_status=status,
                    failure_reason=reason,
                    now=self.ctx.now_iso(),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("source_status_failed error=%s", exc.__class__.__name__)
