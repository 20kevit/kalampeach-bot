"""Shared result objects returned by services.

``action`` values are stable, loggable strings; they never contain user content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ServiceOutcome:
    action: str
    event_id: Optional[int] = None
    clarification_id: Optional[int] = None
    delivery_id: Optional[int] = None
    note: Optional[str] = None

    # Convenience predicates used by the processor and tests.
    @property
    def handled(self) -> bool:
        return self.action not in ("ignored", "duplicate")

    @property
    def ignored(self) -> bool:
        return self.action in ("ignored", "duplicate")
