"""Built-in node types. Importing this package registers them on the default registry."""

from flowforge_engine.nodes.ai import AnthropicNode, GeminiNode, LLMNode, OpenAINode
from flowforge_engine.nodes.http import HTTPRequestNode
from flowforge_engine.nodes.integrations import GmailNode
from flowforge_engine.nodes.io import InputNode, OutputNode, TextNode
from flowforge_engine.nodes.logic import ConditionNode, DelayNode

__all__ = [
    "AnthropicNode",
    "ConditionNode",
    "DelayNode",
    "GeminiNode",
    "GmailNode",
    "HTTPRequestNode",
    "InputNode",
    "LLMNode",
    "OpenAINode",
    "OutputNode",
    "TextNode",
]
