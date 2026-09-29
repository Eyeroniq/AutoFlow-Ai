from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

Operator = Literal[
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "greater_than",
    "greater_or_equal",
    "less_than",
    "less_or_equal",
    "is_empty",
    "is_not_empty",
    "matches",
]
# Operators that ignore the right operand.
UNARY_OPERATORS = frozenset({"is_empty", "is_not_empty"})
_NUMERIC = {
    "greater_than": lambda a, b: a > b,
    "greater_or_equal": lambda a, b: a >= b,
    "less_than": lambda a, b: a < b,
    "less_or_equal": lambda a, b: a <= b,
}

# Runs execute on a worker, so a delay no longer holds an HTTP request open; the per-node
# timeout (WORKFLOW_NODE_TIMEOUT_SECONDS) still applies.
MAX_DELAY_SECONDS = 60


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _fold(value: Any, case_sensitive: bool) -> Any:
    """Lowercase text (and text inside lists) for case-insensitive comparisons."""
    if case_sensitive:
        return value
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return [_fold(v, False) for v in value]
    return value


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def evaluate(left: Any, operator: Operator, right: Any = None, *, case_sensitive: bool = True) -> bool:
    """Numeric strings compare as numbers, so form input "10" equals 10.

    Raises ValueError when the operands can't be compared that way (e.g. 'greater_than'
    on text that isn't a number).
    """
    if operator == "is_empty":
        return is_empty(left)
    if operator == "is_not_empty":
        return not is_empty(left)
    if operator in ("equals", "not_equals"):
        left_num, right_num = _as_number(left), _as_number(right)
        if left_num is not None and right_num is not None:
            equal = left_num == right_num
        else:
            equal = _fold(left, case_sensitive) == _fold(right, case_sensitive)
        return equal if operator == "equals" else not equal
    if operator in ("contains", "not_contains"):
        haystack, needle = _fold(left, case_sensitive), _fold(right, case_sensitive)
        if isinstance(haystack, str):
            found = str(needle) in haystack
        elif isinstance(haystack, (list, tuple, dict)):
            found = needle in haystack
        else:
            raise ValueError(f"'{operator}' needs text, a list, or an object on the left, got {type(left).__name__}")
        return found if operator == "contains" else not found
    if operator in ("starts_with", "ends_with"):
        if not isinstance(left, str):
            raise ValueError(f"'{operator}' needs text on the left, got {type(left).__name__}")
        text, affix = _fold(left, case_sensitive), str(_fold(right, case_sensitive) if right is not None else "")
        return text.startswith(affix) if operator == "starts_with" else text.endswith(affix)
    if operator == "matches":
        if not isinstance(left, str):
            raise ValueError(f"'matches' needs text on the left, got {type(left).__name__}")
        try:
            pattern = re.compile(str(right), 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regular expression {right!r}: {exc}") from None
        return pattern.search(left) is not None
    left_num, right_num = _as_number(left), _as_number(right)
    if left_num is None or right_num is None:
        raise ValueError(f"'{operator}' needs two numbers, got {left!r} and {right!r}")
    return _NUMERIC[operator](left_num, right_num)


class ConditionConfig(NodeConfig):
    left: Any = Field(description="Left operand, typically a reference like {{input.value}}.")
    operator: Operator
    right: Any = Field(default=None, description="Right operand (not used by is_empty / is_not_empty).")
    case_sensitive: bool = Field(
        default=True, description="Off: text compares ignoring case and surrounding spaces (\"Urgent \" equals \"urgent\")."
    )


class ConditionResult(BaseModel):
    result: bool
    branch: Literal["true", "false"]


@register_node("condition")
class ConditionNode(NodeDefinition[ConditionConfig]):
    category = "logic"
    label = "Condition"
    description = "Compares two values and continues down the 'true' or 'false' branch."
    icon = "git-branch"
    config_schema = ConditionConfig
    output_schema = ConditionResult
    branches = ("true", "false")
    portable = True

    async def execute(self, context: NodeContext, config: ConditionConfig) -> NodeResult:
        try:
            result = evaluate(config.left, config.operator, config.right, case_sensitive=config.case_sensitive)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(result=result, branch="true" if result else "false")


class DelayConfig(NodeConfig):
    seconds: float = Field(ge=0, le=MAX_DELAY_SECONDS, description=f"At most {MAX_DELAY_SECONDS}s.")


class DelayResult(BaseModel):
    waited_seconds: float


@register_node("delay")
class DelayNode(NodeDefinition[DelayConfig]):
    category = "logic"
    label = "Delay"
    description = f"Waits a number of seconds (max {MAX_DELAY_SECONDS}) before continuing."
    icon = "timer"
    config_schema = DelayConfig
    output_schema = DelayResult

    async def execute(self, context: NodeContext, config: DelayConfig) -> NodeResult:
        await asyncio.sleep(config.seconds)
        return NodeResult.ok(waited_seconds=config.seconds)
