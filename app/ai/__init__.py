"""AI provider boundary.

The application owns every schema in this package. A provider (currently
NVIDIA) may only return data that validates here; invalid output is rejected
without touching the database or inventing values.
"""

from app.ai.provider import AIProvider, ClarificationContext, ExtractionRequest
from app.ai.schema import (
    AIInvalidOutput,
    CorrectionInterpretation,
    Extraction,
    ManagerIntent,
    validate_correction,
    validate_extraction,
    validate_manager_intent,
    validate_narrative,
)

__all__ = [
    "AIProvider",
    "ClarificationContext",
    "CorrectionInterpretation",
    "ExtractionRequest",
    "ManagerIntent",
    "Extraction",
    "AIInvalidOutput",
    "validate_correction",
    "validate_extraction",
    "validate_manager_intent",
    "validate_narrative",
]
