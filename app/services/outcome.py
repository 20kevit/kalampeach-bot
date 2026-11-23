"""Uniform processing outcome used by the worker to settle inbox rows."""

from __future__ import annotations

from dataclasses import dataclass

DONE = "done"
IGNORED = "ignored"
RETRY = "retry"
FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    kind: str
    detail: str = ""
    category: str | None = None
    retry_after_seconds: int | None = None


def done(detail: str = "") -> Outcome:
    return Outcome(DONE, detail)


def ignored(detail: str = "") -> Outcome:
    return Outcome(IGNORED, detail)


def retry(category: str, detail: str = "", *, after_seconds: int | None = None) -> Outcome:
    return Outcome(RETRY, detail, category, after_seconds)


def failed(category: str, detail: str = "") -> Outcome:
    return Outcome(FAILED, detail, category)
