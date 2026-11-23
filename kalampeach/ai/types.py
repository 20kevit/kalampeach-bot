"""AI provider boundary types.

The application depends on this narrow interface, never on NVIDIA-specific
request/response details (see AI-CONTRACT.md). Two consequences:

* swapping providers means implementing :class:`AIProvider`;
* no service ever receives raw model text, only validated dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# Confidence is advisory only; deterministic rules decide what gets saved.
CONFIDENCE_IGNORED = True


class AIError(Exception):
    """Base class for AI failures with a machine-readable category."""

    category = "ai_error"
    retryable = True

    def __init__(self, message: str, *, category: Optional[str] = None) -> None:
        self.detail = message
        if category is not None:
            self.category = category
        super().__init__("%s: %s" % (self.category, message))


class AIUnavailable(AIError):
    """The provider timed out, returned 5xx, or could not be reached."""

    category = "ai_unavailable"
    retryable = True


class AIConfigurationError(AIError):
    """Provider configuration is invalid (e.g. missing base URL)."""

    category = "ai_configuration"
    retryable = False


class AIInvalidOutput(AIError):
    """The provider returned something the application schema rejects."""

    category = "ai_invalid_output"
    retryable = True


@dataclass(frozen=True)
class ExtractionRequest:
    text: str
    chat_type: str  # 'group' | 'private'
    sender_role: str  # 'employee' | 'manager'
    message_date_local: str  # YYYY-MM-DD (Asia/Tehran)

    def to_payload(self) -> Dict[str, str]:
        return {
            "chat_type": self.chat_type,
            "sender_role": self.sender_role,
            "message_date_local": self.message_date_local,
            "message_text": self.text,
        }


@dataclass(frozen=True)
class ExtractionResult:
    """Validated extraction result; amounts stay decimal strings (never float)."""

    classification: str
    event_type: Optional[str]
    confidence: float
    event_date: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    item_name: Optional[str] = None
    quantity: Optional[str] = None
    unit: Optional[str] = None
    inventory_direction: Optional[str] = None
    description: Optional[str] = None
    missing_fields: Tuple[str, ...] = field(default_factory=tuple)
    ambiguities: Tuple[str, ...] = field(default_factory=tuple)
    reason: Optional[str] = None

    @property
    def is_business_event(self) -> bool:
        return self.classification == "business_event" and self.event_type is not None


@dataclass(frozen=True)
class CorrectionRequest:
    current_snapshot: Dict[str, Optional[str]]
    correction_text: str
    message_date_local: str
    event_type: str


@dataclass(frozen=True)
class CorrectionResult:
    interpretation: str  # 'correction' | 'not_a_correction' | 'unclear'
    changes: Dict[str, Optional[str]]
    reason: Optional[str]
    clarification_question: Optional[str]
    confidence: float

    @property
    def is_correction(self) -> bool:
        return self.interpretation == "correction" and bool(self.changes)


@dataclass(frozen=True)
class ClarificationAnswerRequest:
    original_text: str
    question: str
    missing_fields: Tuple[str, ...]
    answer_text: str
    event_type: str
    message_date_local: str


@dataclass(frozen=True)
class ClarificationAnswerResult:
    """Validated values extracted from an answer to a clarification question."""

    values: Dict[str, Optional[str]]
    missing_fields: Tuple[str, ...]
    ambiguities: Tuple[str, ...]
    reason: Optional[str]
    confidence: float


@dataclass(frozen=True)
class ReportRequestRequest:
    text: str
    today_local: str  # YYYY-MM-DD
    prior_request: Optional[str] = None


@dataclass(frozen=True)
class ReportRequestResult:
    is_report_request: bool
    report_type: Optional[str]  # summary|sales|expenses|inventory|incidents|full
    period_kind: Optional[str]  # today|yesterday|explicit_date|range|last_7_days|this_week|ambiguous
    start_date: Optional[str]
    end_date: Optional[str]
    clarification_question: Optional[str]
    confidence: float


@dataclass(frozen=True)
class NarrativeRequest:
    report_type: str
    period_start: str
    period_end: str
    facts: Dict[str, object]


@dataclass(frozen=True)
class NarrativeResult:
    narrative: str


class AIProvider:
    """Structural interface implemented by adapters (and by test doubles)."""

    name = "abstract"

    def extract_event(self, request: ExtractionRequest) -> ExtractionResult:  # pragma: no cover
        raise NotImplementedError

    def interpret_correction(self, request: CorrectionRequest) -> CorrectionResult:  # pragma: no cover
        raise NotImplementedError

    def complete_clarification(
        self, request: ClarificationAnswerRequest
    ) -> ClarificationAnswerResult:  # pragma: no cover
        raise NotImplementedError

    def parse_report_request(self, request: ReportRequestRequest) -> ReportRequestResult:  # pragma: no cover
        raise NotImplementedError

    def write_narrative(self, request: NarrativeRequest) -> NarrativeResult:  # pragma: no cover
        raise NotImplementedError
