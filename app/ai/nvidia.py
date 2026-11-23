"""NVIDIA provider adapter (OpenAI-compatible chat-completions wire format).

The endpoint, model identifier and structured-output capability are entirely
owner-supplied configuration (``NVIDIA_BASE_URL``, ``NVIDIA_MODEL``,
``NVIDIA_JSON_MODE``). Nothing is hardcoded or inferred, so switching to a
different compatible service only requires configuration.

Failure policy:
* network errors, timeouts, 429 and 5xx are retryable (:class:`AIUnavailable`);
* other HTTP rejections are surfaced as ``AIRequestRejected`` (configuration);
* malformed content gets exactly one repair attempt, then ``AIInvalidOutput``;
* no secret, prompt or message body is ever logged.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any, Callable, Protocol

import requests

from app.ai.prompts import (
    build_correction_messages,
    build_extraction_messages,
    build_intent_messages,
    build_narrative_messages,
    build_repair_messages,
)
from app.ai.provider import ExtractionRequest
from app.ai.schema import (
    CorrectionInterpretation,
    Extraction,
    ManagerIntent,
    validate_correction,
    validate_extraction,
    validate_manager_intent,
    validate_narrative,
)
from app.errors import AIError, AIInvalidOutput, AIUnavailable


class AIRequestRejected(AIError):
    """The provider rejected the request itself (bad endpoint/model/key)."""

    category = "AI_REQUEST_REJECTED"
    retryable = False


class _SessionLike(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


class NvidiaProvider:
    """NVIDIA implementation of :class:`app.ai.provider.AIProvider`."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        json_mode: bool = False,
        session: _SessionLike | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._temperature = temperature
        self._json_mode = json_mode
        self._session = session or requests.Session()
        self._sleep = sleeper

    @property
    def provider_name(self) -> str:
        return "nvidia"

    # ------------------------------------------------------------------ wire
    def _endpoint(self) -> str:
        if self._base_url.endswith("/chat/completions"):
            return self._base_url
        return f"{self._base_url}/chat/completions"

    def _request(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": False,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        attempt = 0
        while True:
            try:
                response = self._session.post(
                    self._endpoint(),
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self._max_retries:
                    raise AIUnavailable("NVIDIA request timed out or is unreachable") from exc
            except requests.RequestException as exc:
                raise AIUnavailable("NVIDIA request failed") from exc
            else:
                status = getattr(response, "status_code", 0)
                if status == 429 or status >= 500:
                    if attempt >= self._max_retries:
                        raise AIUnavailable(f"NVIDIA is unavailable (HTTP {status})")
                elif status >= 400:
                    raise AIRequestRejected(
                        f"NVIDIA rejected the request (HTTP {status}); check "
                        "NVIDIA_BASE_URL, NVIDIA_MODEL and NVIDIA_API_KEY"
                    )
                else:
                    return self._content_of(response)

            attempt += 1
            # Bounded exponential backoff; the worker also bounds total attempts.
            self._sleep(min(2.0 ** attempt * 0.5, 8.0))

    @staticmethod
    def _content_of(response: Any) -> str:
        try:
            body = response.json()
        except ValueError as exc:
            raise AIInvalidOutput("NVIDIA response is not JSON") from exc
        try:
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIInvalidOutput("NVIDIA response has an unexpected shape") from exc
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidOutput("NVIDIA response content is empty")
        return content

    @staticmethod
    def _strict_json(content: str) -> Any:
        """Parse exactly one JSON object; reject fences and surrounding prose."""

        text = content.strip()
        if not (text.startswith("{") and text.endswith("}")):
            raise AIInvalidOutput("AI answer is not a bare JSON object")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIInvalidOutput("AI answer is not valid JSON") from exc

    def _chat_validated(
        self,
        messages: list[dict[str, str]],
        validate: Callable[[Any], Any],
    ) -> Any:
        content = self._request(messages)
        try:
            return validate(self._strict_json(content))
        except AIInvalidOutput:
            # Exactly one bounded repair attempt, never more.
            repair_messages = build_repair_messages(messages=messages, invalid_content=content)
            repaired = self._request(repair_messages)
            try:
                return validate(self._strict_json(repaired))
            except AIInvalidOutput as exc:
                raise AIInvalidOutput(
                    "AI output failed schema validation after one repair attempt"
                ) from exc

    # -------------------------------------------------------------- contract
    def extract_event(self, request: ExtractionRequest) -> Extraction:
        clarification = request.clarification
        messages = build_extraction_messages(
            text=request.text,
            chat_type=request.chat_type,
            message_local_date=request.message_local_date,
            message_local_datetime=request.message_local_datetime,
            clarification_question=clarification.question_text if clarification else None,
            clarification_answer=clarification.answer_text if clarification else None,
            current_snapshot=request.current_snapshot,
        )
        local_date = date.fromisoformat(request.message_local_date)
        return self._chat_validated(
            messages, lambda payload: validate_extraction(payload, message_local_date=local_date)
        )

    def interpret_correction(
        self,
        *,
        snapshot: dict,
        correction_text: str,
        message_local_date: str,
    ) -> CorrectionInterpretation:
        messages = build_correction_messages(
            snapshot=snapshot,
            correction_text=correction_text,
            message_local_date=message_local_date,
        )
        local_date = date.fromisoformat(message_local_date)
        return self._chat_validated(
            messages, lambda payload: validate_correction(payload, message_local_date=local_date)
        )

    def interpret_manager_request(self, *, text: str, today_local: str) -> ManagerIntent:
        messages = build_intent_messages(text=text, today_local=today_local)
        today = date.fromisoformat(today_local)
        # The range limit is enforced again by the reporting service.
        return self._chat_validated(
            messages,
            lambda payload: validate_manager_intent(
                payload, today=today, max_range_days=366
            ),
        )

    def narrate_report(self, *, facts: dict) -> str:
        messages = build_narrative_messages(facts=facts)
        content = self._request(messages)
        return validate_narrative(content)
