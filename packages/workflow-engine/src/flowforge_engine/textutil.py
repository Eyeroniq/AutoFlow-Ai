"""Small text helpers shared by nodes: HTML to plain text, and splitting long messages."""

from __future__ import annotations

import html
import re

_BLOCK_END = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote|pre)>")
_SCRIPTS = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Readable text from an HTML fragment: block ends become line breaks, tags go, entities
    are decoded, and runs of whitespace collapse."""
    markup = _SCRIPTS.sub(" ", markup)
    markup = _BLOCK_END.sub("\n", markup)
    text = html.unescape(_TAG.sub(" ", markup))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def clip(text: str, limit: int) -> tuple[str, bool]:
    """(text cut to `limit` characters with an ellipsis, whether it was cut)."""
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)].rstrip() + "…", True


def split_message(text: str, limit: int) -> list[str]:
    """Split `text` into chunks of at most `limit` characters, preferring paragraph, then
    line, then word boundaries. Empty text gives no chunks."""
    text = text.strip()
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        window = text[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = window.rfind(separator)
            if cut > limit // 3:
                break
        if cut <= 0:
            cut = limit  # one very long word: hard cut
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks
