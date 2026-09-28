"""Built-in node types. Importing this package registers them on the default registry."""

from flowforge_engine.nodes.ai import (
    AnthropicNode,
    GeminiNode,
    GroqNode,
    LLMNode,
    OllamaNode,
    OpenAINode,
    OpenRouterNode,
)
from flowforge_engine.nodes.documents import EntityExtractionNode, OCRNode, PDFExtractNode, SummarizeNode
from flowforge_engine.nodes.http import HTTPRequestNode
from flowforge_engine.nodes.integrations import GmailNode, GmailReadNode
from flowforge_engine.nodes.io import InputNode, OutputNode, TextNode
from flowforge_engine.nodes.logic import ConditionNode, DelayNode

__all__ = [
    "AnthropicNode",
    "ConditionNode",
    "DelayNode",
    "EntityExtractionNode",
    "GeminiNode",
    "GmailNode",
    "GmailReadNode",
    "GroqNode",
    "HTTPRequestNode",
    "InputNode",
    "LLMNode",
    "OCRNode",
    "OllamaNode",
    "OpenAINode",
    "OpenRouterNode",
    "OutputNode",
    "PDFExtractNode",
    "SummarizeNode",
    "TextNode",
]
