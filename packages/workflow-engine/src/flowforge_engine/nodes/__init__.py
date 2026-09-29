"""Built-in node types. Importing this package registers them on the default registry."""

from flowforge_engine.nodes.audio import SpeechToTextNode
from flowforge_engine.nodes.ai import (
    AnthropicNode,
    CerebrasNode,
    CustomLLMNode,
    GeminiNode,
    GroqNode,
    LLMNode,
    MistralNode,
    OllamaNode,
    OpenAINode,
    OpenRouterNode,
)
from flowforge_engine.nodes.documents import EntityExtractionNode, OCRNode, PDFExtractNode, SummarizeNode
from flowforge_engine.nodes.feeds import RSSNode, WebPageNode
from flowforge_engine.nodes.http import HTTPRequestNode
from flowforge_engine.nodes.integrations import GmailNode, GmailReadNode
from flowforge_engine.nodes.io import InputNode, OutputNode, TextNode
from flowforge_engine.nodes.lists import FilterNode, ForEachNode, JoinNode
from flowforge_engine.nodes.logic import ConditionNode, DelayNode
from flowforge_engine.nodes.notify import DiscordWebhookNode, TelegramNode
from flowforge_engine.nodes.search import WebSearchNode
from flowforge_engine.nodes.structured import StructuredOutputNode

__all__ = [
    "AnthropicNode",
    "CerebrasNode",
    "ConditionNode",
    "CustomLLMNode",
    "DelayNode",
    "DiscordWebhookNode",
    "EntityExtractionNode",
    "FilterNode",
    "ForEachNode",
    "GeminiNode",
    "GmailNode",
    "GmailReadNode",
    "GroqNode",
    "HTTPRequestNode",
    "InputNode",
    "JoinNode",
    "LLMNode",
    "MistralNode",
    "OCRNode",
    "OllamaNode",
    "OpenAINode",
    "OpenRouterNode",
    "OutputNode",
    "PDFExtractNode",
    "RSSNode",
    "SpeechToTextNode",
    "StructuredOutputNode",
    "SummarizeNode",
    "TelegramNode",
    "TextNode",
    "WebPageNode",
    "WebSearchNode",
]
