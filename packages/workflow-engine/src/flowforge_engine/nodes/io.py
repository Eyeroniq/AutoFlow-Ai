from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from flowforge_engine.files import FileNotAvailable, file_id_from
from flowforge_engine.models import IDENTIFIER_PATTERN, GraphNode, NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

# "file": the value is an uploaded file's id (POST /api/files); the node outputs the file's
# description ({file_id, filename, content_type, size_bytes}) for document nodes to read.
InputType = Literal["text", "number", "json", "file"]


class InputConfig(NodeConfig):
    name: str | None = Field(
        default=None,
        max_length=100,
        pattern=IDENTIFIER_PATTERN,
        description="Key to read from the run's `inputs`. Defaults to the node id.",
    )
    input_type: InputType = Field(default="text", description="'file' takes an uploaded file's id.")
    default: Any = Field(
        default=None, description="Used when the run doesn't supply this input (for a file: an uploaded file's id)."
    )
    required: bool = True


def coerce_input(value: Any, input_type: InputType) -> Any:
    """Type-check a user-supplied value, accepting numeric/JSON strings from forms."""
    if input_type == "text":
        if not isinstance(value, str):
            raise ValueError(f"expected text, got {type(value).__name__}")
        return value
    if input_type == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number, got a boolean")
        if isinstance(value, (int, float)):
            number = value
        elif isinstance(value, str):
            try:
                number = int(value.strip())
            except ValueError:
                try:
                    number = float(value.strip())
                except ValueError:
                    raise ValueError(f"expected a number, got '{value}'") from None
        else:
            raise ValueError(f"expected a number, got {type(value).__name__}")
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError("expected a finite number")
        return number
    if input_type == "file":
        return file_id_from(value)
    # json
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"expected valid JSON ({exc.msg} at position {exc.pos})") from None
    return value


@register_node("input")
class InputNode(NodeDefinition[InputConfig]):
    category = "io"
    portable = True
    label = "Input"
    description = "Entry point that reads a value from the run's inputs and type-checks it."
    icon = "log-in"
    config_schema = InputConfig

    def output_keys(self, node: GraphNode) -> set[str]:
        name = node.config.get("name") or node.id
        return {"value", name} if isinstance(name, str) else {"value"}

    async def execute(self, context: NodeContext, config: InputConfig) -> NodeResult:
        name = config.name or context.node_id or "value"
        if name in context.inputs:
            raw = context.inputs[name]
        elif config.default is not None:
            raw = config.default
        elif config.required:
            return NodeResult.fail(f"Missing required input '{name}'")
        else:
            return NodeResult(success=True, output={"value": None, name: None})

        try:
            value = coerce_input(raw, config.input_type)
        except ValueError as exc:
            return NodeResult.fail(f"Input '{name}': {exc}")
        if config.input_type == "file":
            try:
                value = (await context.services.files.get(value)).describe()
            except FileNotAvailable as exc:
                return NodeResult.fail(f"Input '{name}': {exc}")
        # Exposed both as {{node.value}} and {{node.<name>}}.
        return NodeResult(success=True, output={"value": value, name: value})


class OutputConfig(NodeConfig):
    name: str = Field(default="result", max_length=100, pattern=IDENTIFIER_PATTERN)
    value: Any = Field(description="Usually a reference such as {{gemini.response}}, or an object of them.")


class OutputResult(BaseModel):
    name: str
    value: Any


@register_node("output")
class OutputNode(NodeDefinition[OutputConfig]):
    category = "io"
    portable = True
    label = "Output"
    description = "Captures a value into the execution's final output under `name`."
    icon = "log-out"
    config_schema = OutputConfig
    output_schema = OutputResult
    produces_final_output = True

    async def execute(self, context: NodeContext, config: OutputConfig) -> NodeResult:
        return NodeResult.ok(name=config.name, value=config.value)


class TextConfig(NodeConfig):
    text: str


class TextResult(BaseModel):
    text: str


@register_node("text")
class TextNode(NodeDefinition[TextConfig]):
    category = "io"
    portable = True
    label = "Text"
    description = "Static text (with {{...}} references resolved) passed downstream."
    icon = "type"
    config_schema = TextConfig
    output_schema = TextResult

    async def execute(self, context: NodeContext, config: TextConfig) -> NodeResult:
        return NodeResult.ok(text=config.text)
