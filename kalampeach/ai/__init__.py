"""AI provider contract and the replaceable NVIDIA adapter.

Only :mod:`kalampeach.ai.types` defines what the rest of the application may ask
for. Swapping providers means implementing that protocol — nothing else in the
codebase imports a vendor SDK.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..config import Settings
from .nvidia import NvidiaProvider
from .types import AIProvider, AIError, AIInvalidOutput, AIUnavailable

__all__ = [
    "AIProvider",
    "AIError",
    "AIInvalidOutput",
    "AIUnavailable",
    "NvidiaProvider",
    "create_provider",
]


def create_provider(
    settings: Settings,
    *,
    transport: Optional[Callable] = None,
) -> AIProvider:
    """Build the configured provider.

    Today only NVIDIA is supported; the factory exists so deployments and tests
    can inject a different implementation without touching service code.
    """

    return NvidiaProvider(settings, transport=transport)
