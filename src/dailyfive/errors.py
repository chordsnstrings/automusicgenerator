"""Exception types. The distinction that matters is retryable vs. not."""


class DailyFiveError(Exception):
    """Base for everything this package raises."""


class ConfigError(DailyFiveError):
    """A required setting is missing or malformed. Never retryable."""


class ProviderError(DailyFiveError):
    """An upstream API failed.

    ``retryable`` is what the Conductor branches on: a 500 or a timeout is
    worth another attempt, a 400 or a content rejection is not.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool = False,
                 status: int | None = None, code: int | None = None):
        self.provider = provider
        self.retryable = retryable
        self.status = status
        self.code = code
        super().__init__(f"[{provider}] {message}")


class BudgetExceeded(DailyFiveError):
    """The run would spend past its daily cap. Aborts rather than overspending."""


class RejectedContent(DailyFiveError):
    """Suno's moderation refused the lyrics or style. Retrying as-is never helps."""


class QCFailure(DailyFiveError):
    """A clip failed measurement. Carries the reason so the Archivist can log it."""

    def __init__(self, reason: str, metrics: dict | None = None):
        self.reason = reason
        self.metrics = metrics or {}
        super().__init__(reason)
