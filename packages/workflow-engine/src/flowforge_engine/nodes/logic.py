from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

Operator = Literal["equals", "not_equals", "contains", "greater_than", "less_than"]

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


def evaluate(left: Any, operator: Operator, right: Any) -> bool:
    """Numeric strings compare as numbers, so form input "10" equals 10."""
    if operator in ("equals", "not_equals"):
        left_num, right_num = _as_number(left), _as_number(right)
        equal = left_num == right_num if left_num is not None and right_num is not None else left == right
        return equal if operator == "equals" else not equal
    if operator == "contains":
        if isinstance(left, str):
            return str(right) in left
        if isinstance(left, (list, tuple, dict)):
            return right in left
        raise ValueError(f"'contains' needs text, a list, or an object on the left, got {type(left).__name__}")
    left_num, right_num = _as_number(left), _as_number(right)
    if left_num is None or right_num is None:
        raise ValueError(f"'{operator}' needs two numbers, got {left!r} and {right!r}")
    return left_num > right_num if operator == "greater_than" else left_num < right_num


class ConditionConfig(NodeConfig):
    left: Any = Field(description="Left operand, typically a reference like {{input.value}}.")
    operator: Operator
    right: Any = Field(description="Right operand.")


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
            result = evaluate(config.left, config.operator, config.right)
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
