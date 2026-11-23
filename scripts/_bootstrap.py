"""Shared bootstrap for the cPanel cron/CLI scripts.

Adds the repository root to ``sys.path`` so ``python scripts/<name>.py`` works
without installing the package, and provides small helpers for consistent CLI
behaviour (exit codes, safe error output).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DATABASE = 3
EXIT_FAILURE = 1


def repo_root() -> Path:
    return REPO_ROOT


def fail(message: str, *, exit_code: int = EXIT_FAILURE) -> "NoReturn":  # noqa: F821
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)
