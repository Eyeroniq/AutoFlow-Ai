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
    """An LLM or integration provider call failed in an expected way (API error, refusal)."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(f"{provider}: {message}")
