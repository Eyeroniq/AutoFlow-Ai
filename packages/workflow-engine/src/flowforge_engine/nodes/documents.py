"""Document AI nodes: PDF text extraction, OCR, summarization, and entity extraction.

PDF Extract and OCR read uploaded files (flowforge_engine.files) and run on the "ocr"
queue: they are CPU-bound, so their work runs in a thread (the event loop keeps
heartbeats and stop checks going) and checks for a stop between pages. Summarize and
Entity Extraction call an LLM through the provider abstraction, with the same
provider/model/fallback chain as the LLM nodes, on the "llm" queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from flowforge_engine.files import IMAGE_TYPES, PDF, FileNotAvailable, StoredFile, file_id_from
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.ai import (
    MAX_FALLBACKS,
    LLMChainFailed,
    generate_with_fallback,
    llm_required_providers,
    parse_chain_entry,
)
from flowforge_engine.providers.settings import LLM_PROVIDER_NAMES, LLMProviderName
from flowforge_engine.registry import GuardedLLMConfig, NodeConfig, NodeDefinition, register_node

logger = logging.getLogger(__name__)

FileRef = str | dict[str, Any]
FILE_DESCRIPTION = "The file: a reference like {{input.document}} (an Input node of type file) or an uploaded file's id."
# Tells the editor to render a file picker (uploads) next to the reference field.
FILE_FIELD: dict[str, Any] = {"format": "file-ref"}
PAGES_DESCRIPTION = "Pages to read, 1-based: e.g. '1-3,5' or '2-' (to the end). Blank reads every page."
_PAGE_SPEC = re.compile(r"^\s*\d+\s*(-\s*\d*\s*)?(,\s*\d+\s*(-\s*\d*\s*)?)*$")

T = TypeVar("T")


class _Cancelled(Exception):
    """The node was cancelled (a stop) while its thread was working."""


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """0-based page indexes for a 1-based spec like '1-3,5' or '4-'. None/blank = all."""
    if spec is None or not spec.strip():
        return list(range(page_count))
    pages: set[int] = set()
    for part in spec.split(","):
        start_text, dash, end_text = part.strip().partition("-")
        start = int(start_text)
        end = (int(end_text) if end_text.strip() else page_count) if dash else start
        if start < 1 or end < start:
            raise ValueError(f"invalid page range '{part.strip()}'")
        if start > page_count:
            raise ValueError(f"page {start} is past the end (the document has {page_count} page{'s' * (page_count != 1)})")
        pages.update(range(start - 1, min(end, page_count)))
    return sorted(pages)


def _check_page_spec(value: str | None) -> str | None:
    if value is not None and value.strip() and not _PAGE_SPEC.match(value):
        raise ValueError("use page numbers and ranges like '1-3,5' or '2-'")
    return value or None


async def _in_thread(work: Callable[[threading.Event], T]) -> T:
    """Run CPU-bound `work(cancel)` in a thread; on cancellation, set `cancel` so the work
    stops at its next check (a page boundary) instead of running on unobserved."""
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(work, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise


async def _open_file(context: NodeContext, ref: FileRef, allowed: frozenset[str] | set[str]) -> StoredFile:
    try:
        stored = await context.services.files.get(file_id_from(ref))
    except ValueError as exc:
        raise FileNotAvailable(str(exc)) from None
    if stored.content_type not in allowed:
        raise FileNotAvailable(
            f"'{stored.filename}' is {stored.content_type}; this node reads {', '.join(sorted(allowed))}"
        )
    return stored


# --- PDF Extract -----------------------------------------------------------------------


class PDFExtractConfig(NodeConfig):
    file: FileRef = Field(description=FILE_DESCRIPTION, json_schema_extra=FILE_FIELD)
    pages: str | None = Field(default=None, description=PAGES_DESCRIPTION)
    max_chars: int = Field(default=200_000, ge=1, le=2_000_000, description="Stop collecting text after this many characters.")

    @field_validator("pages")
    @classmethod
    def _page_spec(cls, value: str | None) -> str | None:
        return _check_page_spec(value)


class PageText(BaseModel):
    page: int
    text: str
    chars: int


class PDFExtractResult(BaseModel):
    text: str
    pages: list[PageText]
    page_count: int
    pages_extracted: list[int]
    char_count: int
    truncated: bool
    # Little or no text layer: a scanned document, which needs the OCR node instead.
    needs_ocr: bool
    filename: str


def _extract_pdf(path: str, spec: str | None, max_chars: int, cancel: threading.Event) -> dict[str, Any]:
    import pymupdf

    with pymupdf.open(path) as document:
        indexes = parse_pages(spec, document.page_count)
        pages, total, truncated = [], 0, False
        for index in indexes:
            if cancel.is_set():
                raise _Cancelled
            text = document[index].get_text("text").strip()
            if total + len(text) > max_chars:
                text, truncated = text[: max(0, max_chars - total)], True
            total += len(text)
            pages.append({"page": index + 1, "text": text, "chars": len(text)})
            if truncated:
                break
        return {"pages": pages, "page_count": document.page_count, "char_count": total, "truncated": truncated}


@register_node("pdf_extract")
class PDFExtractNode(NodeDefinition[PDFExtractConfig]):
    category = "documents"
    label = "PDF Extract"
    description = "Extracts the text layer of a PDF (PyMuPDF), optionally from a page range."
    icon = "file-text"
    config_schema = PDFExtractConfig
    output_schema = PDFExtractResult
    queue = "ocr"

    async def execute(self, context: NodeContext, config: PDFExtractConfig) -> NodeResult:
        try:
            stored = await _open_file(context, config.file, {PDF})
            data = await _in_thread(lambda cancel: _extract_pdf(str(stored.path), config.pages, config.max_chars, cancel))
        except (FileNotAvailable, ValueError) as exc:
            return NodeResult.fail(str(exc))
        except RuntimeError as exc:  # PyMuPDF: damaged or encrypted file
            return NodeResult.fail(f"Could not read the PDF: {exc}")
        pages = data["pages"]
        return NodeResult.ok(
            text="\n\n".join(p["text"] for p in pages if p["text"]),
            pages=pages,
            page_count=data["page_count"],
            pages_extracted=[p["page"] for p in pages],
            char_count=data["char_count"],
            truncated=data["truncated"],
            needs_ocr=bool(pages) and data["char_count"] < 25 * len(pages),
            filename=stored.filename,
        )


# --- OCR ---------------------------------------------------------------------------------


class OCRConfig(NodeConfig):
    file: FileRef = Field(description=FILE_DESCRIPTION, json_schema_extra=FILE_FIELD)
    language: str = Field(
        default="eng",
        pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$",
        description="Tesseract language code(s), e.g. 'eng', 'deu', or 'eng+fra'. The worker image ships eng, deu, fra, spa, ita, por.",
    )
    pages: str | None = Field(default=None, description=PAGES_DESCRIPTION + " (PDFs and multi-page TIFFs)")
    dpi: int = Field(default=300, ge=72, le=600, description="Resolution PDF pages are rendered at before OCR.")
    psm: int = Field(default=3, ge=0, le=13, description="Tesseract page segmentation mode (3 = automatic).")
    max_pages: int = Field(default=50, ge=1, le=500, description="Refuse documents with more pages than this.")
    prefer_text_layer: bool = Field(
        default=False,
        description="PDFs: read pages that already have a text layer directly (like PDF Extract) and OCR only the scanned ones.",
    )

    @field_validator("pages")
    @classmethod
    def _page_spec(cls, value: str | None) -> str | None:
        return _check_page_spec(value)


# A PDF page with at least this much text in its text layer isn't treated as a scan.
MIN_TEXT_LAYER_CHARS = 25


class OCRPage(BaseModel):
    page: int
    text: str
    chars: int
    confidence: float | None
    # "ocr", or "text_layer" when prefer_text_layer read the PDF's own text.
    method: str = "ocr"


class OCRResult(BaseModel):
    text: str
    pages: list[OCRPage]
    page_count: int
    pages_processed: list[int]
    mean_confidence: float | None
    language: str
    engine: str
    source: Literal["pdf", "image"]
    filename: str


def _ocr_image(image: Any, language: str, psm: int) -> tuple[str, float | None]:
    """Text (lines and paragraphs kept) and mean word confidence for one page image."""
    import pytesseract

    data = pytesseract.image_to_data(
        image, lang=language, config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=180
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(word)
        confidence = float(data["conf"][i])
        if confidence >= 0:
            confidences.append(confidence)
    paragraphs: dict[tuple[int, int], list[str]] = {}
    for (block, par, _line), words in lines.items():
        paragraphs.setdefault((block, par), []).append(" ".join(words))
    text = "\n\n".join("\n".join(par_lines) for par_lines in paragraphs.values())
    mean = round(sum(confidences) / len(confidences), 1) if confidences else None
    return text, mean


def _run_ocr(stored: StoredFile, config: OCRConfig, cancel: threading.Event) -> dict[str, Any]:
    import pytesseract
    from PIL import Image, ImageSequence

    installed = set(pytesseract.get_languages(config=""))
    missing = [code for code in config.language.split("+") if code not in installed]
    if missing:
        raise ValueError(
            f"Tesseract language data not installed: {', '.join(missing)} (installed: {', '.join(sorted(installed - {'osd'}))})"
        )

    pages = []
    if stored.content_type == PDF:
        import pymupdf

        source = "pdf"
        with pymupdf.open(stored.path) as document:
            page_count = document.page_count
            indexes = parse_pages(config.pages, page_count)
            if len(indexes) > config.max_pages:
                raise ValueError(f"{len(indexes)} pages to OCR is more than max_pages ({config.max_pages})")
            for index in indexes:
                if cancel.is_set():
                    raise _Cancelled
                if config.prefer_text_layer:
                    layer = document[index].get_text("text").strip()
                    if len(layer) >= MIN_TEXT_LAYER_CHARS:
                        pages.append({"page": index + 1, "text": layer, "chars": len(layer), "confidence": None,
                                      "method": "text_layer"})
                        continue
                pixmap = document[index].get_pixmap(dpi=config.dpi, colorspace=pymupdf.csGRAY)
                image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
                text, confidence = _ocr_image(image, config.language, config.psm)
                pages.append({"page": index + 1, "text": text, "chars": len(text), "confidence": confidence, "method": "ocr"})
    else:
        source = "image"
        with Image.open(stored.path) as image:
            frames = [frame.copy() for frame in ImageSequence.Iterator(image)]
        page_count = len(frames)
        indexes = parse_pages(config.pages, page_count)
        if len(indexes) > config.max_pages:
            raise ValueError(f"{len(indexes)} pages to OCR is more than max_pages ({config.max_pages})")
        for index in indexes:
            if cancel.is_set():
                raise _Cancelled
            text, confidence = _ocr_image(frames[index].convert("L"), config.language, config.psm)
            pages.append({"page": index + 1, "text": text, "chars": len(text), "confidence": confidence, "method": "ocr"})

    scored = [p["confidence"] for p in pages if p["confidence"] is not None]
    return {
        "pages": pages,
        "page_count": page_count,
        "mean_confidence": round(sum(scored) / len(scored), 1) if scored else None,
        "engine": f"tesseract {pytesseract.get_tesseract_version()}",
        "source": source,
    }


@register_node("ocr")
class OCRNode(NodeDefinition[OCRConfig]):
    category = "documents"
    label = "OCR"
    description = "Reads text from images and scanned PDFs with Tesseract (pages rendered first)."
    icon = "scan-text"
    config_schema = OCRConfig
    output_schema = OCRResult
    queue = "ocr"

    async def execute(self, context: NodeContext, config: OCRConfig) -> NodeResult:
        try:
            stored = await _open_file(context, config.file, {PDF, *IMAGE_TYPES})
            data = await _in_thread(lambda cancel: _run_ocr(stored, config, cancel))
        except (FileNotAvailable, ValueError) as exc:
            return NodeResult.fail(str(exc))
        except RuntimeError as exc:  # pytesseract timeouts / PyMuPDF read errors
            return NodeResult.fail(f"OCR failed: {exc}")
        except OSError as exc:  # Pillow can't decode the image
            return NodeResult.fail(f"Could not read the image: {exc}")
        pages = data["pages"]
        logger.info(
            "OCR finished",
            extra={"node_id": context.node_id, "pages": len(pages), "mean_confidence": data["mean_confidence"]},
        )
        return NodeResult.ok(
            text="\n\n".join(p["text"] for p in pages if p["text"]),
            pages=pages,
            page_count=data["page_count"],
            pages_processed=[p["page"] for p in pages],
            mean_confidence=data["mean_confidence"],
            language=config.language,
            engine=data["engine"],
            source=data["source"],
            filename=stored.filename,
        )


# --- LLM document nodes -----------------------------------------------------------------


class LLMChainConfig(GuardedLLMConfig):
    """Provider settings shared by the LLM document nodes (same semantics as the LLM nodes)."""

    provider: LLMProviderName = Field(default="gemini", description="Which LLM service answers. 'mock' returns a canned reply.")
    model: str | None = Field(default=None, description="Model id. Blank uses the provider's default.")
    fallback: list[str] = Field(
        default_factory=list,
        max_length=MAX_FALLBACKS,
        description="Providers to try in order if this one fails, each 'provider' or 'provider:model'.",
    )
    max_tokens: int = Field(default=8192, ge=1, le=65536, description="Includes thinking for models that think (Gemini 2.5+).")
    max_input_chars: int = Field(
        default=60_000, ge=1_000, le=500_000, description="Longer text is cut to this many characters (reported as truncated)."
    )

    @field_validator("fallback")
    @classmethod
    def _known_providers(cls, entries: list[str]) -> list[str]:
        for entry in entries:
            provider, _ = parse_chain_entry(entry)
            if provider not in LLM_PROVIDER_NAMES:
                raise ValueError(f"unknown provider '{provider}' (known: {', '.join(LLM_PROVIDER_NAMES)})")
        return entries


class _LLMDocumentNode:
    """Mixin: credential checks for the provider chain, and input truncation."""

    config_schema: type[LLMChainConfig]

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return llm_required_providers(node, str(self.config_schema.model_fields["provider"].default))


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


# Summarize ------------------------------------------------------------------------------

_LENGTHS = {
    "short": "2-3 sentences (about 50 words)",
    "medium": "one solid paragraph (about 120 words)",
    "long": "a few paragraphs (about 300 words)",
}
_STYLES = {
    "paragraph": "flowing prose",
    "bullets": "a bulleted list of the key points, one line each",
    "executive": "an executive summary: a one-line headline, then the key points, then any decisions, deadlines, or actions",
}


class SummarizeConfig(LLMChainConfig):
    text: str = Field(min_length=1, description="What to summarize, usually {{ocr.text}} or {{pdf.text}}.")
    length: Literal["short", "medium", "long"] = "medium"
    style: Literal["paragraph", "bullets", "executive"] = "paragraph"
    focus: str = Field(default="", description="Optional: what to emphasize, e.g. 'payment terms and deadlines'.")
    language: str = Field(default="", description="Language of the summary. Blank: the document's language.")
    temperature: float = Field(default=0.3, ge=0, le=2)
    stream: bool = Field(default=False, description="Stream the summary to live watchers as it's written.")


class SummarizeResult(BaseModel):
    summary: str
    length: str
    style: str
    input_chars: int
    truncated: bool
    provider: str
    provider_used: str
    model: str
    mock: bool
    fallback_errors: list[dict[str, Any]]


@register_node("summarize")
class SummarizeNode(_LLMDocumentNode, NodeDefinition[SummarizeConfig]):
    category = "documents"
    guard_fields = ('text', 'focus')
    label = "Summarize"
    description = "Summarizes document text with an LLM: length, style, focus, and a fallback chain."
    icon = "text-quote"
    config_schema = SummarizeConfig
    output_schema = SummarizeResult
    queue = "llm"

    async def execute(self, context: NodeContext, config: SummarizeConfig) -> NodeResult:
        text, truncated = _clip(config.text.strip(), config.max_input_chars)
        if not text:
            return NodeResult.fail("There is no text to summarize (the input is empty)")
        instructions = [
            f"Summarize the document below as {_STYLES[config.style]}, in {_LENGTHS[config.length]}.",
            "Only use facts stated in the document; don't add anything.",
        ]
        if config.focus.strip():
            instructions.append(f"Focus on: {config.focus.strip()}.")
        instructions.append(
            f"Write it in {config.language.strip()}." if config.language.strip() else "Write it in the document's language."
        )
        if truncated:
            instructions.append("The document was cut short; summarize what is there.")
        try:
            answer = await generate_with_fallback(
                context,
                provider=config.provider,
                model=config.model,
                fallback=config.fallback,
                system_prompt="You are a precise summarizer of business and technical documents. "
                "Reply with the summary only, without a preamble.",
                user_prompt=" ".join(instructions) + f"\n\n<document>\n{text}\n</document>",
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                stream=config.stream,
            )
        except LLMChainFailed as exc:
            return NodeResult.fail(str(exc), fallback_errors=exc.errors)
        return NodeResult.ok(
            summary=answer.text.strip(),
            length=config.length,
            style=config.style,
            input_chars=len(text),
            truncated=truncated,
            provider=config.provider,
            provider_used=answer.provider_used,
            model=answer.model,
            mock=answer.mock,
            fallback_errors=answer.fallback_errors,
        )


# Entity Extraction ----------------------------------------------------------------------

EntityType = Literal["people", "organizations", "dates", "amounts"]
_CUSTOM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ISO_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _loose_number(value: Any) -> Any:
    """'$1,250.00' -> 1250.0; leaves anything unparseable for validation to reject."""
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
        try:
            return float(cleaned) if cleaned not in {"", "-", "."} else None
        except ValueError:
            return value
    return value


class _Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Person(_Entity):
    name: str = Field(min_length=1)
    role: str | None = None


class Organization(_Entity):
    name: str = Field(min_length=1)
    kind: str | None = None


class DateEntity(_Entity):
    text: str = Field(min_length=1)
    iso: str | None = None
    meaning: str | None = None

    @field_validator("iso")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        if value is not None and value.strip() and not _ISO_DATE.match(value.strip()):
            raise ValueError("must be YYYY, YYYY-MM, or YYYY-MM-DD (or null)")
        return value.strip() if value and value.strip() else None


class Amount(_Entity):
    text: str = Field(min_length=1)
    value: float | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code, e.g. USD")
    meaning: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        return _loose_number(value)


class CustomEntity(_Entity):
    text: str = Field(min_length=1)
    note: str | None = None


class Entities(BaseModel):
    """What the model must return (validated; one retry with the errors on failure)."""

    model_config = ConfigDict(extra="ignore")

    people: list[Person] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)
    dates: list[DateEntity] = Field(default_factory=list)
    amounts: list[Amount] = Field(default_factory=list)
    custom: dict[str, list[CustomEntity]] = Field(default_factory=dict)


def parse_custom_types(entries: list[str]) -> dict[str, str]:
    """["invoice_number: the invoice id", "po_number"] -> {name: description}."""
    parsed = {}
    for entry in entries:
        name, _, description = entry.partition(":")
        name = name.strip().lower().replace(" ", "_")
        if not _CUSTOM_NAME.match(name):
            raise ValueError(f"custom type '{entry}': the name must be lowercase letters, digits, and _")
        parsed[name] = description.strip()
    return parsed


class EntityExtractionConfig(LLMChainConfig):
    text: str = Field(min_length=1, description="Document text, usually {{ocr.text}} or {{pdf.text}}.")
    entity_types: list[EntityType] = Field(
        default_factory=lambda: ["people", "organizations", "dates", "amounts"],
        description="Which built-in entity types to extract.",
    )
    custom_types: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Extra types, each 'name' or 'name: what it is', e.g. 'invoice_number: the invoice or reference number'.",
    )
    temperature: float = Field(default=0.0, ge=0, le=2)

    @field_validator("custom_types")
    @classmethod
    def _custom(cls, entries: list[str]) -> list[str]:
        parse_custom_types(entries)
        return entries


class EntityExtractionResult(BaseModel):
    entities: dict[str, Any]
    counts: dict[str, int]
    attempts: int
    input_chars: int
    truncated: bool
    provider: str
    provider_used: str
    model: str
    mock: bool
    fallback_errors: list[dict[str, Any]]


_SCHEMA_TEXT = {
    "people": '"people": [{"name": "Full Name", "role": "their role, or null"}]',
    "organizations": '"organizations": [{"name": "Org Name", "kind": "company / agency / bank / ..., or null"}]',
    "dates": '"dates": [{"text": "as written", "iso": "YYYY-MM-DD, YYYY-MM, YYYY, or null", "meaning": "e.g. invoice date, or null"}]',
    "amounts": '"amounts": [{"text": "as written", "value": 1234.5, "currency": "ISO 4217 code or null", "meaning": "e.g. total due, or null"}]',
}


def extraction_prompt(text: str, types: list[str], custom: dict[str, str], truncated: bool) -> str:
    fields = [_SCHEMA_TEXT[t] for t in types]
    if custom:
        fields.append('"custom": {' + ", ".join(f'"{name}": [{{"text": "as written", "note": "or null"}}]' for name in custom) + "}")
    lines = [
        "Extract entities from the document below. Reply with a single JSON object with exactly these keys:",
        "{" + ", ".join(fields) + "}",
        "Rules: include every distinct entity that is stated in the document, once each; use empty lists when there are "
        "none; never invent values; numbers are plain JSON numbers without symbols or thousands separators.",
    ]
    for name, description in custom.items():
        lines.append(f"- custom.{name}: {description or name.replace('_', ' ')}")
    if truncated:
        lines.append("The document was cut short; extract from what is there.")
    return "\n".join(lines) + f"\n\n<document>\n{text}\n</document>"


def parse_entities(reply: str, types: list[str], custom: dict[str, str]) -> Entities:
    """The model's reply as validated Entities. Raises ValueError explaining what's wrong."""
    body = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end < start:
        raise ValueError("the reply contains no JSON object")
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the reply is not valid JSON ({exc.msg} at position {exc.pos})") from None
    if not isinstance(data, dict):
        raise ValueError("the reply must be a JSON object")
    try:
        entities = Entities.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8])
        raise ValueError(f"the JSON doesn't match the schema: {problems}") from None
    # Keep only what was asked for.
    for entity_type in ("people", "organizations", "dates", "amounts"):
        if entity_type not in types:
            setattr(entities, entity_type, [])
    entities.custom = {name: entities.custom.get(name, []) for name in custom}
    return entities


@register_node("extract_entities")
class EntityExtractionNode(_LLMDocumentNode, NodeDefinition[EntityExtractionConfig]):
    category = "documents"
    guard_fields = ('text',)
    label = "Entity Extraction"
    description = "Pulls people, organizations, dates, amounts, and custom types out of text as validated JSON."
    icon = "tags"
    config_schema = EntityExtractionConfig
    output_schema = EntityExtractionResult
    queue = "llm"

    async def execute(self, context: NodeContext, config: EntityExtractionConfig) -> NodeResult:
        text, truncated = _clip(config.text.strip(), config.max_input_chars)
        if not text:
            return NodeResult.fail("There is no text to extract entities from (the input is empty)")
        types = list(dict.fromkeys(config.entity_types))
        custom = parse_custom_types(config.custom_types)
        prompt = extraction_prompt(text, types, custom, truncated)
        system = "You extract structured data from documents. You reply with JSON only: no prose, no code fences."
        fallback_errors: list[dict[str, Any]] = []
        problem = ""
        for attempt in (1, 2):
            user_prompt = prompt if attempt == 1 else (
                f"{prompt}\n\nYour previous reply could not be used: {problem}. "
                "Reply again with only the JSON object, exactly as specified."
            )
            try:
                answer = await generate_with_fallback(
                    context,
                    provider=config.provider,
                    model=config.model,
                    fallback=config.fallback,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                )
            except LLMChainFailed as exc:
                return NodeResult.fail(str(exc), fallback_errors=fallback_errors + exc.errors, attempts=attempt)
            fallback_errors += answer.fallback_errors
            try:
                entities = parse_entities(answer.text, types, custom)
            except ValueError as exc:
                problem = str(exc)
                logger.warning(
                    "entity reply was invalid",
                    extra={"node_id": context.node_id, "attempt": attempt, "problem": problem},
                )
                continue
            data = entities.model_dump(mode="json")
            counts = {t: len(data[t]) for t in types} | {f"custom.{n}": len(v) for n, v in data["custom"].items()}
            return NodeResult.ok(
                entities={t: data[t] for t in types} | ({"custom": data["custom"]} if custom else {}),
                counts=counts,
                attempts=attempt,
                input_chars=len(text),
                truncated=truncated,
                provider=config.provider,
                provider_used=answer.provider_used,
                model=answer.model,
                mock=answer.mock,
                fallback_errors=fallback_errors,
            )
        return NodeResult.fail(
            f"The model's reply didn't match the entity schema after 2 attempts: {problem}",
            attempts=2,
            raw_reply=answer.text[:4000],
            fallback_errors=fallback_errors,
        )
