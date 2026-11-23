"""Telegram Bot API client (HTTPS only, standard library transport).

The client distinguishes three failure classes, because retry safety differs:

* ``retryable``: Telegram did not accept the message (429/5xx, connection
  refused before sending) — safe to retry;
* ``uncertain``: the request may have been delivered (timeout, malformed 200
  response) — the caller must not blindly resend;
* neither: a permanent rejection (blocked bot, chat not found, invalid token).

The request URL contains the bot token, so it is never logged. Logs carry only
the method name, chat id, and error category.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Settings

logger = logging.getLogger("kalampeach.telegram")

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]

MAX_MESSAGE_LENGTH = 4096
DEFAULT_CHUNK_LIMIT = 3800


class TelegramError(Exception):
    """Telegram API or transport failure with a safe, loggable category."""

    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
        retry_after: Optional[int] = None,
    ) -> None:
        self.category = category
        self.retryable = retryable
        self.uncertain = uncertain
        self.retry_after = retry_after
        super().__init__(category)


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - defensive
            payload = b""
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        raise TelegramError("telegram_network_error", retryable=True) from exc
    except TimeoutError as exc:
        raise TelegramError("telegram_timeout", uncertain=True) from exc
    except OSError as exc:
        raise TelegramError("telegram_network_error", retryable=True) from exc


class TelegramClient:
    """Small synchronous client with bounded timeouts."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[Transport] = None,
        timeout: float = 10.0,
    ) -> None:
        self._settings = settings
        self._transport = transport or urllib_transport
        self._timeout = timeout
        self._base = settings.telegram_api_base_url.rstrip("/")

    # -- public API --------------------------------------------------------
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> int:
        """Send one text message; return the Telegram message id."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must not be empty")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError("message text exceeds Telegram's 4096 character limit")
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": bool(disable_notification),
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        result = self._call("sendMessage", payload)
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            # Telegram accepted the request but the response is unusable.
            raise TelegramError("telegram_invalid_response", uncertain=True)
        return int(message_id)

    def get_webhook_info(self) -> Dict[str, Any]:
        return self._call("getWebhookInfo", {})

    def set_webhook(self, url: str, secret_token: str, allowed_updates: Optional[List[str]] = None) -> bool:
        payload: Dict[str, Any] = {
            "url": url,
            "secret_token": secret_token,
            "max_connections": 10,
        }
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        return bool(self._call("setWebhook", payload))

    def delete_webhook(self) -> bool:
        return bool(self._call("deleteWebhook", {"drop_pending_updates": False}))

    def get_me(self) -> Dict[str, Any]:
        return self._call("getMe", {})

    # -- internals ---------------------------------------------------------
    def _call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = "%s/bot%s/%s" % (self._base, self._settings.telegram_bot_token, method)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, raw = self._transport(url, headers, body, self._timeout)
        except TelegramError:
            raise
        except TimeoutError as exc:
            raise TelegramError("telegram_timeout", uncertain=True) from exc
        except OSError as exc:
            raise TelegramError("telegram_network_error", retryable=True) from exc

        document = _decode(raw)
        if status == 429:
            retry_after = _retry_after(document)
            raise TelegramError("telegram_rate_limited", retryable=True, retry_after=retry_after)
        if status >= 500:
            raise TelegramError("telegram_unavailable", retryable=True)
        if status >= 400:
            raise _classify_client_error(document)
        if document is None:
            raise TelegramError("telegram_invalid_response", uncertain=True)
        if document.get("ok") is not True:
            raise _classify_client_error(document)
        result = document.get("result")
        return result if isinstance(result, dict) else {"result": result}


def _decode(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _retry_after(document: Optional[Dict[str, Any]]) -> Optional[int]:
    if not document:
        return None
    parameters = document.get("parameters")
    if isinstance(parameters, dict):
        value = parameters.get("retry_after")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _classify_client_error(document: Optional[Dict[str, Any]]) -> TelegramError:
    description = ""
    if document:
        value = document.get("description")
        if isinstance(value, str):
            description = value.lower()

    if "bot was blocked" in description or "user is deactivated" in description:
        return TelegramError("recipient_unavailable")
    if "chat not found" in description:
        return TelegramError("recipient_not_found")
    if "bot was kicked" in description or "not enough rights" in description:
        return TelegramError("bot_removed_or_forbidden")
    if "chat_write_forbidden" in description or "have no rights" in description:
        return TelegramError("bot_removed_or_forbidden")
    if "message is too long" in description:
        return TelegramError("message_too_long")
    if "message to reply not found" in description:
        return TelegramError("reply_target_missing")
    if "unauthorized" in description or "invalid token" in description:
        return TelegramError("invalid_token")
    return TelegramError("telegram_rejected")


def chunk_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> List[str]:
    """Split a long report into Telegram-sized chunks on natural boundaries."""

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if limit <= 0 or limit > MAX_MESSAGE_LENGTH:
        raise ValueError("limit must be between 1 and 4096")
    if not text.strip():
        return []
    remaining = text.strip("\n")
    if len(remaining) <= limit:
        return [remaining] if remaining else []

    chunks: List[str] = []
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"))
        if split_at < limit // 2:
            # No good boundary; cut on whitespace, else hard-cut.
            split_at = window.rfind(" ")
        if split_at < limit // 2:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining.rstrip())
    return chunks
