"""Processing error taxonomy shared by the processor, worker and webhook."""

from __future__ import annotations

from typing import Optional


class ProcessingError(Exception):
    """Base class for a failed update; ``category`` is safe to persist/log."""

    category = "processing_error"
    retryable = True

    def __init__(self, category: Optional[str] = None, *, source_message_id: Optional[int] = None) -> None:
        if category:
            self.category = category
        self.source_message_id = source_message_id
        super().__init__(self.category)


class RetryableProcessingError(ProcessingError):
    retryable = True


class PermanentProcessingError(ProcessingError):
    retryable = False


# Frequently used categories (documented in CPANEL-DEPLOYMENT.md).
INVALID_PAYLOAD = "invalid_payload"
AI_UNAVAILABLE = "ai_unavailable"
AI_INVALID_OUTPUT = "ai_invalid_output"
DATABASE_ERROR = "database_error"
TELEGRAM_UNAVAILABLE = "telegram_unavailable"
UNEXPECTED = "unexpected_error"
