"""Built-in node types. Importing this package registers them on the default registry."""

from flowforge_engine.nodes.audio import SpeechToTextNode
from flowforge_engine.nodes.calendar import CalendarNode
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
from flowforge_engine.nodes.knowledge import AddDocumentNode, ChunkerNode, EmbeddingNode, RerankerNode, RetrieverNode
from flowforge_engine.nodes.io import InputNode, OutputNode, TextNode
from flowforge_engine.nodes.lists import FilterNode, ForEachNode, JoinNode
from flowforge_engine.nodes.logic import ConditionNode, DelayNode
from flowforge_engine.nodes.notify import DiscordWebhookNode, TelegramNode
from flowforge_engine.nodes.privacy import RedactImageNode, RedactNode, RestoreNode, SecretScannerNode
from flowforge_engine.nodes.search import WebSearchNode
from flowforge_engine.nodes.structured import StructuredOutputNode
from flowforge_engine.nodes.vision import VisionNode
from flowforge_engine.nodes.workspace import (
    AirtableCreateNode,
    AirtableListNode,
    NotionCreatePageNode,
    NotionQueryNode,
)

__all__ = [
    "AddDocumentNode",
    "AirtableCreateNode",
    "AirtableListNode",
    "AnthropicNode",
    "CalendarNode",
    "CerebrasNode",
    "ChunkerNode",
    "ConditionNode",
    "CustomLLMNode",
    "DelayNode",
    "DiscordWebhookNode",
    "EmbeddingNode",
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
    "NotionCreatePageNode",
    "NotionQueryNode",
    "MistralNode",
    "OCRNode",
    "OllamaNode",
    "OpenAINode",
    "OpenRouterNode",
    "OutputNode",
    "PDFExtractNode",
    "RedactImageNode",
    "RedactNode",
    "RerankerNode",
    "RestoreNode",
    "RetrieverNode",
    "RSSNode",
    "SecretScannerNode",
    "SpeechToTextNode",
    "StructuredOutputNode",
    "SummarizeNode",
    "TelegramNode",
    "TextNode",
    "VisionNode",
    "WebPageNode",
    "WebSearchNode",
]
