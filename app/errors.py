"""Application error taxonomy.

Every error carries a stable ``category`` string that is persisted on queue
rows, report deliveries and work logs. Categories are safe to log and to show
to operators; they never contain credentials or message bodies.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected application errors."""

    category = "APP_ERROR"
    retryable = False


class ConfigError(AppError):
    """Deployment configuration is missing or invalid.

    Only key names and rule descriptions are included, never values.
    """

    category = "CONFIG_INVALID"

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("Invalid configuration: " + " | ".join(self.problems))


class DomainValidationError(AppError):
    """Structured data failed the application's deterministic business rules."""

    category = "DOMAIN_VALIDATION"

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("Validation failed: " + " | ".join(self.problems))


class AIError(AppError):
    """Provider failure, always safe to log."""

    category = "AI_ERROR"


class AIUnavailable(AIError):
    """Timeout, connection failure, rate limit or provider 5xx."""

    category = "AI_UNAVAILABLE"
    retryable = True


class AIInvalidOutput(AIError):
    """The provider returned output that failed strict validation."""

    category = "AI_INVALID_OUTPUT"
    retryable = False


class TelegramError(AppError):
    """Telegram Bot API failure."""

    category = "TELEGRAM_ERROR"


class TelegramUnavailable(TelegramError):
    """Timeout, connection failure or Telegram 5xx."""

    category = "TELEGRAM_UNAVAILABLE"
    retryable = True


class TelegramRateLimited(TelegramUnavailable):
    category = "TELEGRAM_RATE_LIMITED"
    retryable = True


class TelegramForbidden(TelegramError):
    """The bot may not message this chat (blocked / never started)."""

    category = "TELEGRAM_FORBIDDEN"
    retryable = False

    def __init__(self, message: str, *, cannot_initiate: bool = False) -> None:
        self.cannot_initiate = cannot_initiate
        super().__init__(message)


class TelegramInvalidRequest(TelegramError):
    """Telegram rejected the request as malformed (non-retryable)."""

    category = "TELEGRAM_INVALID_REQUEST"
    retryable = False


class DeliveryUncertain(TelegramUnavailable):
    """A send timed out; Telegram may or may not have accepted it.

    Treated conservatively: the delivery is not retried automatically because a
    blind retry could post a duplicate report to the manager.
    """

    category = "DELIVERY_UNCERTAIN"


class SchemaError(AppError):
    """Database schema is missing, outdated or unreadable."""

    category = "SCHEMA_ERROR"


class InboxError(AppError):
    """Durable inbox problem."""

    category = "INBOX_ERROR"
