"""Minimal Telegram Bot API client.

Only the methods the MVP needs are implemented: ``sendMessage``,
``getWebhookInfo``, ``setWebhook`` and ``deleteWebhook``. Sends are bounded by
a timeout, classified into retryable/permanent categories, and never log the
message text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests

from app.errors import (
    DeliveryUncertain,
    TelegramForbidden,
    TelegramInvalidRequest,
    TelegramRateLimited,
    TelegramUnavailable,
)

MAX_MESSAGE_CHARS = 4096
_CHUNK_CHARS = 3800
_MAX_CHUNKS = 4
_UNCERTAIN_CODES = ("bot can't initiate conversation", "bot was blocked by the user", "user is deactivated")


class _SessionLike(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class SendResult:
    message_id: int | None
    chunk_count: int


def split_message(text: str, *, limit: int = _CHUNK_CHARS, max_chunks: int = _MAX_CHUNKS) -> list[str]:
    """Split a long report on line boundaries, never mid-word if avoidable."""

    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= limit:
            chunks.append(remaining)
            remaining = ""
            break
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append("…" + remaining[: limit - 2])
    return [chunk for chunk in chunks if chunk]


class TelegramClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
        session: _SessionLike | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ wire
    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        uncertain_on_timeout: bool,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.Timeout as exc:
            if uncertain_on_timeout:
                raise DeliveryUncertain("Telegram send timed out") from exc
            raise TelegramUnavailable("Telegram request timed out") from exc
        except requests.RequestException as exc:
            raise TelegramUnavailable("Telegram request failed") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if getattr(response, "status_code", 200) == 200 and body.get("ok"):
            return body

        error_code = body.get("error_code") if isinstance(body, dict) else None
        description = str((body or {}).get("description") or "")
        lowered = description.lower()

        if error_code == 429:
            retry_after = 0
            parameters = body.get("parameters") if isinstance(body, dict) else None
            if isinstance(parameters, dict):
                retry_after = int(parameters.get("retry_after") or 0)
            raise TelegramRateLimited(f"Telegram rate limit (retry_after={retry_after})")

        if error_code == 403 or any(code in lowered for code in _UNCERTAIN_CODES):
            raise TelegramForbidden(
                "Telegram refused delivery (bot blocked or chat not started)",
                cannot_initiate=True,
            )

        if error_code is not None and int(error_code) >= 500:
            raise TelegramUnavailable(f"Telegram is unavailable (HTTP {error_code})")

        raise TelegramInvalidRequest(
            f"Telegram rejected the request (error_code={error_code})"
        )

    # -------------------------------------------------------------- contract
    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        chunks = split_message(text)
        first_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_to_message_id is not None and index == 0:
                payload["reply_parameters"] = {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            body = self._call("sendMessage", payload, uncertain_on_timeout=True)
            if first_message_id is None:
                result = body.get("result") if isinstance(body, dict) else None
                if isinstance(result, dict):
                    value = result.get("message_id")
                    if isinstance(value, int):
                        first_message_id = value
        return SendResult(message_id=first_message_id, chunk_count=len(chunks))

    def get_webhook_info(self) -> dict[str, Any]:
        body = self._call("getWebhookInfo", {}, uncertain_on_timeout=False)
        result = body.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "secret_token": secret_token,
            "drop_pending_updates": False,
            "allowed_updates": allowed_updates or ["message"],
        }
        body = self._call("setWebhook", payload, uncertain_on_timeout=False)
        result = body.get("result")
        return {"ok": True, "result": result}

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        body = self._call(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
            uncertain_on_timeout=False,
        )
        return {"ok": True, "result": body.get("result")}

    def describe(self) -> dict[str, Any]:
        """Secret-free description, safe for logs and diagnostics."""

        return {"base_url": self._base_url, "timeout_seconds": self._timeout}
