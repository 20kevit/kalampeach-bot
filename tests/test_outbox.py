"""TEST-PLAN B/C: Telegram transport classification, chunking, secret safety."""

from __future__ import annotations

import json
import logging

import pytest

from kalampeach.services.notify import Notifier
from kalampeach.telegram.client import (
    DEFAULT_CHUNK_LIMIT,
    TelegramClient,
    TelegramError,
    chunk_text,
)
from conftest import EMPLOYEE_ID


class Recorder:
    """Injectable transport: records calls and returns a scripted response."""

    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = body if body is not None else {"ok": True, "result": {"message_id": 42}}
        self.error = error
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode("utf-8")
        return self.status, raw


def client(settings, transport):
    return TelegramClient(settings, transport=transport, timeout=5.0)


def test_send_message_posts_json_and_returns_message_id(settings):
    transport = Recorder(body={"ok": True, "result": {"message_id": 4242}})
    message_id = client(settings, transport).send_message(EMPLOYEE_ID, "سلام")

    assert message_id == 4242
    call = transport.calls[0]
    assert call["url"] == "https://api.telegram.org/bot%s/sendMessage" % settings.telegram_bot_token
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["timeout"] == 5.0
    payload = json.loads(call["body"].decode("utf-8"))
    assert payload == {
        "chat_id": EMPLOYEE_ID,
        "text": "سلام",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }


def test_reply_payload_is_forwarded(settings):
    transport = Recorder()
    client(settings, transport).send_message(EMPLOYEE_ID, "پاسخ", reply_to_message_id=77)
    payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert payload["reply_to_message_id"] == 77
    assert payload["allow_sending_without_reply"] is True


def test_out_of_range_text_is_rejected_before_any_request(settings):
    transport = Recorder()
    telegram = client(settings, transport)
    with pytest.raises(ValueError):
        telegram.send_message(EMPLOYEE_ID, "   ")
    with pytest.raises(ValueError):
        telegram.send_message(EMPLOYEE_ID, "x" * 4097)
    assert transport.calls == []


def test_rate_limit_is_retryable_with_retry_after(settings):
    transport = Recorder(
        status=429,
        body={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7},
        },
    )
    with pytest.raises(TelegramError) as info:
        client(settings, transport).send_message(EMPLOYEE_ID, "سلام")
    error = info.value
    assert error.category == "telegram_rate_limited"
    assert error.retryable is True
    assert error.uncertain is False
    assert error.retry_after == 7


def test_server_errors_are_retryable(settings):
    with pytest.raises(TelegramError) as info:
        client(settings, Recorder(status=502, body=b"<html>bad gateway</html>")).send_message(
            EMPLOYEE_ID, "سلام"
        )
    assert (info.value.category, info.value.retryable) == ("telegram_unavailable", True)


@pytest.mark.parametrize(
    "description, category",
    [
        ("Forbidden: bot was blocked by the user", "recipient_unavailable"),
        ("Forbidden: user is deactivated", "recipient_unavailable"),
        ("Bad Request: chat not found", "recipient_not_found"),
        ("Forbidden: bot was kicked from the group chat", "bot_removed_or_forbidden"),
        ("Bad Request: message is too long", "message_too_long"),
        ("Bad Request: message to reply not found", "reply_target_missing"),
        ("Unauthorized", "invalid_token"),
        ("Bad Request: something odd", "telegram_rejected"),
    ],
)
def test_permanent_errors_are_classified_and_not_retried(settings, description, category):
    with pytest.raises(TelegramError) as info:
        client(
            settings,
            Recorder(status=400, body={"ok": False, "description": description}),
        ).send_message(EMPLOYEE_ID, "سلام")
    assert info.value.category == category
    assert info.value.retryable is False
    assert info.value.uncertain is False


def test_ok_false_body_is_classified(settings):
    with pytest.raises(TelegramError) as info:
        client(
            settings,
            Recorder(status=200, body={"ok": False, "description": "Bad Request: chat not found"}),
        ).send_message(EMPLOYEE_ID, "سلام")
    assert info.value.category == "recipient_not_found"


def test_unreadable_success_response_is_uncertain(settings):
    with pytest.raises(TelegramError) as info:
        client(settings, Recorder(status=200, body=b"not-json")).send_message(EMPLOYEE_ID, "سلام")
    assert (info.value.category, info.value.uncertain) == ("telegram_invalid_response", True)


def test_missing_message_id_is_uncertain(settings):
    with pytest.raises(TelegramError) as info:
        client(settings, Recorder(body={"ok": True, "result": {}})).send_message(
            EMPLOYEE_ID, "سلام"
        )
    assert info.value.uncertain is True


def test_timeout_is_uncertain_and_connection_errors_are_retryable(settings):
    with pytest.raises(TelegramError) as timeout:
        client(settings, Recorder(error=TimeoutError())).send_message(EMPLOYEE_ID, "سلام")
    assert (timeout.value.category, timeout.value.uncertain) == ("telegram_timeout", True)

    with pytest.raises(TelegramError) as refused:
        client(settings, Recorder(error=ConnectionResetError())).send_message(EMPLOYEE_ID, "سلام")
    assert (refused.value.category, refused.value.retryable) == (
        "telegram_network_error",
        True,
    )


def test_token_and_message_text_are_never_logged(settings, caplog):
    caplog.set_level(logging.DEBUG)
    secret_token = settings.telegram_bot_token
    private_text = "فروش محرمانه ۱۲۳۴۵۶"

    with pytest.raises(TelegramError):
        client(
            settings,
            Recorder(status=400, body={"ok": False, "description": "Bad Request: chat not found"}),
        ).send_message(EMPLOYEE_ID, private_text)

    # Exercise the failure logging of the notifier as well.
    transport = Recorder(error=ConnectionResetError())
    telegram = client(settings, transport)

    class Ctx:
        ai = None

        def __init__(self):
            self.settings = settings
            self.telegram = telegram

        def now(self):
            from datetime import datetime, timezone

            return datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

        def now_iso(self):
            from kalampeach.timeutil import to_utc_iso

            return to_utc_iso(self.now())

    assert Notifier(Ctx()).send(EMPLOYEE_ID, private_text) is None

    combined = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [str(record.args) for record in caplog.records]
    )
    assert secret_token not in combined
    assert private_text not in combined
    assert "telegram_network_error" in combined, "the safe category is still logged"


def test_webhook_management_calls(settings):
    transport = Recorder(body={"ok": True, "result": True})
    telegram = client(settings, transport)
    assert telegram.set_webhook(
        "https://example.test/webhook", "s3cret", allowed_updates=["message"]
    ) is True
    payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert payload["url"] == "https://example.test/webhook"
    assert payload["secret_token"] == "s3cret"
    assert payload["allowed_updates"] == ["message"]
    assert "s3cret" not in transport.calls[0]["url"]

    transport = Recorder(body={"ok": True, "result": {"url": ""}})
    assert client(settings, transport).get_webhook_info() == {"url": ""}


def test_chunk_text_keeps_short_text_intact():
    assert chunk_text("یک پیام کوتاه") == ["یک پیام کوتاه"]
    assert chunk_text("") == []
    assert chunk_text("   \n ") == []


def test_chunk_text_limits_and_boundaries():
    paragraph = "خط اول.\n\n" * 2000
    chunks = chunk_text(paragraph)
    assert len(chunks) > 1
    assert all(len(chunk) <= DEFAULT_CHUNK_LIMIT for chunk in chunks)
    assert all(chunk.strip() == chunk for chunk in chunks)
    assert chunks[0].endswith("خط اول.")


def test_chunk_text_hard_cuts_a_single_long_token():
    chunks = chunk_text("x" * 9000)
    assert all(len(chunk) <= DEFAULT_CHUNK_LIMIT for chunk in chunks)
    assert "".join(chunks) == "x" * 9000


def test_chunk_text_rejects_impossible_limits():
    with pytest.raises(ValueError):
        chunk_text("text", limit=0)
    with pytest.raises(ValueError):
        chunk_text("text", limit=5000)
    with pytest.raises(ValueError):
        chunk_text(None)  # type: ignore[arg-type]


def test_notifier_records_provenance_for_reply_resolution(ctx):
    from helpers import seed_event

    event_id = seed_event(ctx, 1, amount="1000")
    message_id = Notifier(ctx).send(
        EMPLOYEE_ID, "✅ ثبت شد.", kind="event_confirmation", event_id=int(event_id)
    )
    assert message_id is not None
    with ctx.db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM bot_messages WHERE chat_id = ? AND message_id = ?",
            (EMPLOYEE_ID, message_id),
        ).fetchone()
    assert row["kind"] == "event_confirmation"
    assert row["event_id"] == int(event_id)


def test_notifier_failure_never_raises(ctx, fake_telegram):
    fake_telegram.queue_failure(TelegramError("bot_removed_or_forbidden"))
    assert Notifier(ctx).send(EMPLOYEE_ID, "سلام") is None
    with ctx.db.connection() as connection:
        total = connection.execute("SELECT COUNT(*) AS total FROM bot_messages").fetchone()["total"]
    assert total == 0
