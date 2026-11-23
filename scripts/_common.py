"""Shared helpers for operational scripts (not used by the web app)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.bootstrap import build_context  # noqa: E402
from app.context import AppContext  # noqa: E402


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--env-file",
        default=os.environ.get("KALAMPEACH_ENV_FILE") or os.environ.get("ENV_FILE"),
        help="private KEY=VALUE file with deployment configuration",
    )
    return parser


def context_from_args(args: argparse.Namespace, *, configure_logs: bool = True) -> AppContext:
    environ = dict(os.environ)
    if args.env_file:
        environ["KALAMPEACH_ENV_FILE"] = args.env_file
        environ.pop("ENV_FILE", None)
    return build_context(environ=environ, configure_logs=configure_logs)


def fail(message: str, *, code: int = 1) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return code
