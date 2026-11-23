"""NVIDIA provider adapter.

The adapter speaks the OpenAI-compatible Chat Completions wire format, because
that is the interface NVIDIA exposes. **No endpoint and no model ID are
hardcoded**: both come from validated configuration supplied and verified by the
owner (see DECISIONS.md).

Failure policy (AI-CONTRACT.md section 7):

* bounded timeout per request;
* bounded retries for timeouts, transport errors, 429 and 5xx only;
* exactly one bounded "repair" attempt when the model output fails schema
  validation;
* no fallback to free-text or guessed extraction — failures raise ``AIError``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from ..config import Settings
from . import prompts, schema
from .types import (
    AIConfigurationError,
    AIError,
    AIInvalidOutput,
    AIProvider,
    AIUnavailable,
    ClarificationAnswerRequest,
    ClarificationAnswerResult,
    CorrectionRequest,
    CorrectionResult,
    ExtractionRequest,
    ExtractionResult,
    NarrativeRequest,
    NarrativeResult,
    ReportRequestRequest,
    ReportRequestResult,
)

logger = logging.getLogger("kalampeach.ai.nvidia")

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]
Recorder = Callable[[str, str, Optional[str], Optional[int]], None]


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    """Minimal HTTPS POST transport (no third-party HTTP client needed)."""

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx carry a body we may inspect
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - defensive
            payload = b""
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        raise AIUnavailable("network error contacting provider: %s" % exc.__class__.__name__) from exc
    except TimeoutError as exc:
        raise AIUnavailable("provider request timed out") from exc
    except OSError as exc:
        raise AIUnavailable("provider connection failed: %s" % exc.__class__.__name__) from exc


class NvidiaProvider(AIProvider):
    name = "nvidia"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[Transport] = None,
        recorder: Optional[Recorder] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.nvidia_base_url or not settings.nvidia_model:
            raise AIConfigurationError("NVIDIA_BASE_URL and NVIDIA_MODEL must be configured")
        if not settings.nvidia_api_key:
            raise AIConfigurationError("NVIDIA_API_KEY must be configured")
        self._settings = settings
        self._transport = transport or urllib_transport
        self._recorder = recorder
        self._sleep = sleeper
        self._url = _completions_url(settings.nvidia_base_url)

    # -- public operations -------------------------------------------------
    def extract_event(self, request: ExtractionRequest) -> ExtractionResult:
        return self._execute(
            "extract_event",
            prompts.EXTRACTION_SYSTEM,
            prompts.extraction_user_prompt(
                text=request.text,
                chat_type=request.chat_type,
                sender_role=request.sender_role,
                message_date_local=request.message_date_local,
            ),
            schema.validate_extraction,
            max_tokens=700,
        )

    def interpret_correction(self, request: CorrectionRequest) -> CorrectionResult:
        return self._execute(
            "interpret_correction",
            prompts.CORRECTION_SYSTEM,
            prompts.correction_user_prompt(
                current_snapshot=request.current_snapshot,
                event_type=request.event_type,
                message_date_local=request.message_date_local,
                text=request.correction_text,
            ),
            schema.validate_correction,
            max_tokens=700,
        )

    def complete_clarification(
        self, request: ClarificationAnswerRequest
    ) -> ClarificationAnswerResult:
        return self._execute(
            "complete_clarification",
            prompts.CLARIFICATION_SYSTEM,
            prompts.clarification_user_prompt(
                original_text=request.original_text,
                question=request.question,
                missing_fields=request.missing_fields,
                answer_text=request.answer_text,
                event_type=request.event_type,
                message_date_local=request.message_date_local,
            ),
            schema.validate_clarification_answer,
            max_tokens=500,
        )

    def parse_report_request(self, request: ReportRequestRequest) -> ReportRequestResult:
        return self._execute(
            "parse_report_request",
            prompts.REPORT_REQUEST_SYSTEM,
            prompts.report_request_user_prompt(
                text=request.text,
                today_local=request.today_local,
                prior_request=request.prior_request,
            ),
            schema.validate_report_request,
            max_tokens=400,
        )

    def write_narrative(self, request: NarrativeRequest) -> NarrativeResult:
        return self._execute(
            "write_narrative",
            prompts.NARRATIVE_SYSTEM,
            prompts.narrative_user_prompt(facts=request.facts),
            schema.validate_narrative,
            max_tokens=700,
        )

    # -- internals ---------------------------------------------------------
    def _execute(self, operation, system_prompt, user_prompt, validator, *, max_tokens):
        total_attempts = 1 + max(0, int(self._settings.nvidia_max_retries))
        transport_failures = 0
        repair_used = False
        prompt = user_prompt
        started = time.monotonic()

        while transport_failures < total_attempts:
            try:
                content = self._request_once(operation, system_prompt, prompt, max_tokens)
                result = validator(schema.parse_json_object(content))
                self._record(operation, "ok", None, started)
                return result
            except AIInvalidOutput as exc:
                if not repair_used:
                    repair_used = True
                    logger.warning(
                        "ai_invalid_output operation=%s reason=%s", operation, exc.category
                    )
                    prompt = prompts.repair_prompt(user_prompt, str(exc)[:200])
                    continue
                self._record(operation, "invalid", exc.category, started)
                raise
            except AIUnavailable as exc:
                transport_failures += 1
                if transport_failures >= total_attempts:
                    self._record(operation, "unavailable", exc.category, started)
                    raise
                delay = min(1.0 * (2 ** (transport_failures - 1)), 4.0)
                logger.warning(
                    "ai_transport_retry operation=%s attempt=%d/%d category=%s",
                    operation,
                    transport_failures,
                    total_attempts,
                    exc.category,
                )
                self._sleep(delay)
            except AIError as exc:
                self._record(operation, "error", exc.category, started)
                raise
        raise AIUnavailable("provider retries exhausted")  # pragma: no cover

    def _request_once(self, operation, system_prompt, user_prompt, max_tokens) -> str:
        payload = {
            "model": self._settings.nvidia_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        if self._settings.nvidia_response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": "Bearer %s" % self._settings.nvidia_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            status, raw = self._transport(
                self._url, headers, body, float(self._settings.nvidia_timeout_seconds)
            )
        except AIError:
            raise
        except TimeoutError as exc:
            raise AIUnavailable("provider request timed out") from exc
        except OSError as exc:
            raise AIUnavailable("provider connection failed: %s" % exc.__class__.__name__) from exc

        if status == 429:
            raise AIUnavailable("provider rate limited the request", category="ai_rate_limited")
        if status >= 500:
            raise AIUnavailable("provider returned HTTP %d" % status)
        if status >= 400:
            raise AIError(
                "provider rejected the request with HTTP %d" % status,
                category="ai_http_error",
            )

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIInvalidOutput("provider response is not JSON") from exc

        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIInvalidOutput("provider response contains no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise AIInvalidOutput("provider response contains no message")
        content = message.get("content")
        if isinstance(content, list):  # tolerate content parts from compatible gateways
            parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidOutput("provider response contains no text content")
        return content

    def _record(self, operation: str, outcome: str, category: Optional[str], started: float) -> None:
        if self._recorder is None:
            return
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            self._recorder(operation, outcome, category, elapsed_ms)
        except Exception:  # pragma: no cover - audit must never break extraction
            logger.warning("ai_recorder_failed operation=%s", operation)


def _completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"
