from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flowforge_engine.models import ValidationIssue


class EngineError(Exception):
    """Base class for all workflow engine errors."""


class GraphError(EngineError):
    """The graph is structurally unusable (e.g. an edge points at a missing node)."""


class CycleError(GraphError):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__("Cycle detected: " + " → ".join(cycle))


class VariableResolutionError(EngineError):
    """A {{...}} reference could not be resolved against the available scope."""

    def __init__(self, expression: str, reason: str):
        self.expression = expression
        self.reason = reason
        super().__init__(f"Cannot resolve '{{{{{expression}}}}}': {reason}")


class GraphValidationFailed(EngineError):
    """Raised by execute_graph when the graph does not pass validate_graph."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        summary = "; ".join(issue.message for issue in issues[:3])
        more = f" (+{len(issues) - 3} more)" if len(issues) > 3 else ""
        super().__init__(f"Workflow graph is invalid: {summary}{more}")


class ProviderError(EngineError):
    """An LLM or integration provider call failed in an expected way (API error, refusal).

    `retryable` marks transient failures (429, 5xx, network) that the retry helper may
    repeat; `retry_after` is the provider's requested wait in seconds, when it sent one.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.attempts = 1
        super().__init__(f"{provider}: {message}")

    def gave_up(self, attempts: int, reason: str | None = None) -> ProviderError:
        """Annotate the error with how many attempts were made before giving up."""
        self.attempts = attempts
        suffix = f"gave up after {attempts} attempt{'s' if attempts != 1 else ''}"
        self.args = (f"{self.args[0]} ({suffix}{'; ' + reason if reason else ''})",)
        return self


class MissingCredentialsError(ProviderError):
    """A real provider was requested but no credentials are configured for it."""

    def __init__(self, provider: str, hint: str):
        super().__init__(provider, hint)
        # Stable wording: validation messages and API clients key off "Authentication missing".
        self.args = (f"Authentication missing for provider '{provider}': {hint}",)


class ProviderNotSupportedError(ProviderError, NotImplementedError):
    """The provider doesn't offer this capability (e.g. embeddings on Groq)."""
