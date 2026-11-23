"""Provider-independent AI interface.

Business code depends on this protocol only. ``app.ai.nvidia`` implements it;
tests use an in-memory fake. Replacing the provider requires no changes to the
event, clarification, correction or reporting services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.ai.schema import CorrectionInterpretation, Extraction, ManagerIntent


@dataclass(frozen=True)
class ClarificationContext:
    """Information supplied when re-interpreting an answer to a question."""

    missing_fields: tuple[str, ...]
    question_text: str
    answer_text: str
    original_text: str


@dataclass(frozen=True)
class ExtractionRequest:
    text: str
    chat_type: str
    message_local_date: str
    message_local_datetime: str
    clarification: ClarificationContext | None = None
    current_snapshot: dict | None = field(default=None)


@runtime_checkable
class AIProvider(Protocol):
    """Narrow operations exposed to the application."""

    def extract_event(self, request: ExtractionRequest) -> Extraction:
        """Classify and extract one message. Raises AIError subclasses."""

    def interpret_correction(
        self,
        *,
        snapshot: dict,
        correction_text: str,
        message_local_date: str,
    ) -> CorrectionInterpretation:
        """Propose a patch for an existing event."""

    def interpret_manager_request(self, *, text: str, today_local: str) -> ManagerIntent:
        """Decide whether a manager message asks for a report, and for which range."""

    def narrate_report(self, *, facts: dict) -> str:
        """Return a short Persian narrative limited to the supplied facts."""

    @property
    def provider_name(self) -> str: ...
