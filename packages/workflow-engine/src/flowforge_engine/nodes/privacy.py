"""Privacy nodes: find secrets and personal data, and redact text reversibly.

All three call flowforge_engine.privacy (the same detection service as the privacy guard
and the masking of stored step data). Outputs never contain matched values: the scanner
reports types and positions, and Redact keeps its {placeholder: original} mapping in the
run's vault (encrypted Redis with a one-hour expiry in the API), returning only a
`mapping_id` that Restore takes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.privacy import (
    ALL_CATEGORIES,
    describe,
    presidio_status,
    pseudonymize,
    restore,
    scan,
    scan_text,
    summarize,
)
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

PERSONAL_HELP = "Also find names, emails, phone numbers, and locations (Presidio + rules). Off: secrets, cards, Aadhaar, PAN only."


def _categories(include_personal: bool) -> frozenset[str]:
    return ALL_CATEGORIES if include_personal else ALL_CATEGORIES - {"personal"}


# --- Secret Scanner --------------------------------------------------------------------------


class SecretScannerConfig(NodeConfig):
    data: Any = Field(description="What to scan: text, or any JSON (every string inside is scanned), e.g. {{email.value}}.")
    include_personal_data: bool = Field(default=True, description=PERSONAL_HELP)
    fail_on_findings: bool = Field(default=False, description="Fail the node (and stop the run) when anything is found.")


class SecretScannerResult(BaseModel):
    findings: list[dict[str, Any]]
    count: int
    summary: str
    clean: bool
    by_type: dict[str, int]
    by_category: dict[str, int]
    personal_data_engine: str


@register_node("secret_scanner")
class SecretScannerNode(NodeDefinition[SecretScannerConfig]):
    category = "privacy"
    label = "Secret Scanner"
    description = "Finds secrets (API keys, tokens, passwords), card numbers, Aadhaar, PAN, and personal data; reports types and positions, never the values."
    icon = "shield-alert"
    config_schema = SecretScannerConfig
    output_schema = SecretScannerResult

    async def execute(self, context: NodeContext, config: SecretScannerConfig) -> NodeResult:
        categories = _categories(config.include_personal_data)
        allowlist = context.services.privacy.allowlist
        findings = await asyncio.to_thread(scan, config.data, categories=categories, allowlist=allowlist)
        summary = summarize(findings)
        out = {
            "findings": [f.as_dict() for f in findings], "count": len(findings), "clean": not findings,
            # "1 card number, 1 Aadhaar number" (no values): for messages.
            "summary": describe(findings) or "nothing sensitive",
            "by_type": summary["by_type"], "by_category": summary["by_category"],
            "personal_data_engine": "rules" if not config.include_personal_data else (
                "presidio" if presidio_status() is None else "rules (Presidio unavailable)"
            ),
        }
        if findings and config.fail_on_findings:
            return NodeResult.fail(f"Found {describe(findings)}", **out)
        return NodeResult.ok(**out)


# --- PII Redact / Restore --------------------------------------------------------------------


class RedactConfig(NodeConfig):
    text: str = Field(description="The text to redact, e.g. {{email.value.body_text}}.")
    include_personal_data: bool = Field(default=True, description=PERSONAL_HELP)


class RedactResult(BaseModel):
    text: str
    mapping_id: str | None
    count: int
    placeholders: list[str]
    by_type: dict[str, int]


@register_node("pii_redact")
class RedactNode(NodeDefinition[RedactConfig]):
    category = "privacy"
    label = "PII Redact"
    description = "Replaces personal data and secrets with placeholders like <PERSON_1>, so an LLM never sees them; PII Restore puts them back."
    icon = "eye-off"
    config_schema = RedactConfig
    output_schema = RedactResult

    async def execute(self, context: NodeContext, config: RedactConfig) -> NodeResult:
        findings = await asyncio.to_thread(
            scan_text, config.text, categories=_categories(config.include_personal_data),
            allowlist=context.services.privacy.allowlist,
        )
        text, mapping = pseudonymize(config.text, findings)
        mapping_id = await context.services.vault.put(mapping) if mapping else None
        return NodeResult.ok(
            text=text, mapping_id=mapping_id, count=len(findings), placeholders=list(mapping),
            by_type=summarize(findings)["by_type"],
        )


class RestoreConfig(NodeConfig):
    text: str = Field(description="Text with placeholders, e.g. the LLM's answer {{gemini.response}}.")
    mapping_id: str | None = Field(
        default=None, description="The PII Redact node's mapping, e.g. {{pii_redact.mapping_id}} (blank: nothing to restore)."
    )
    fail_on_unknown: bool = Field(
        default=False, description="Fail if the text has placeholders the mapping doesn't know (e.g. the LLM invented <PERSON_9>)."
    )


class RestoreResult(BaseModel):
    text: str
    restored: int
    unknown_placeholders: list[str]


@register_node("pii_restore")
class RestoreNode(NodeDefinition[RestoreConfig]):
    category = "privacy"
    label = "PII Restore"
    description = "Puts the original values back in place of PII Redact's placeholders (within an hour of the redaction)."
    icon = "eye"
    config_schema = RestoreConfig
    output_schema = RestoreResult

    async def execute(self, context: NodeContext, config: RestoreConfig) -> NodeResult:
        if not config.mapping_id:
            return NodeResult.ok(text=config.text, restored=0, unknown_placeholders=[])
        mapping = await context.services.vault.get(config.mapping_id)
        if mapping is None:
            return NodeResult.fail(
                "The redaction mapping has expired or doesn't exist (mappings are kept for an hour, for the run that made them)"
            )
        text, restored, unknown = restore(config.text, mapping)
        if unknown and config.fail_on_unknown:
            return NodeResult.fail(f"Unknown placeholders in the text: {', '.join(unknown)}", restored=restored)
        return NodeResult.ok(text=text, restored=restored, unknown_placeholders=unknown)


# --- Redact Image ----------------------------------------------------------------------------


class RedactImageConfig(NodeConfig):
    image: Any = Field(
        description="The screenshot or photo, e.g. {{input.image}} (an Input node of type file).",
        json_schema_extra={"format": "file-ref"},
    )
    include_personal_data: bool = Field(default=True, description=PERSONAL_HELP)
    language: str = Field(
        default="eng", pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$", description="Tesseract language(s), e.g. eng or eng+hin."
    )
    padding: int = Field(default=4, ge=0, le=40, description="Extra pixels around each hidden word.")


class RedactImageResult(BaseModel):
    file: dict[str, Any]
    count: int
    hidden_words: int
    summary: str
    by_type: dict[str, int]
    width: int
    height: int


# Small screenshots are OCR'd at this scale (Tesseract reads ~30px-high text best).
_MIN_OCR_WIDTH = 1600
MAX_IMAGE_PIXELS = 40_000_000


def redact_image(path: Any, categories: frozenset[str], allowlist: list[str], language: str, padding: int):
    """(PNG bytes with every sensitive word covered by a black box, findings, words hidden,
    size). Words come from Tesseract with their boxes; each line's text is scanned, and the
    words a finding overlaps are covered."""
    import io

    import pytesseract
    from PIL import Image, ImageDraw, ImageOps

    with Image.open(path) as opened:
        opened.seek(0)
        image = ImageOps.exif_transpose(opened).convert("RGB")
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ValueError(f"the image is {image.width}x{image.height}; at most {MAX_IMAGE_PIXELS // 1_000_000} megapixels")
    scale = max(1.0, _MIN_OCR_WIDTH / image.width)
    ocr_input = image.resize((round(image.width * scale), round(image.height * scale))) if scale > 1 else image
    data = pytesseract.image_to_data(ocr_input, lang=language, config="--psm 3",
                                     output_type=pytesseract.Output.DICT, timeout=180)

    # Rebuild each line's text, remembering where every word sits in it.
    lines: dict[tuple[int, int, int], list[int]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip():
            lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    findings, boxes = [], []
    for indexes in lines.values():
        text, spans = "", []
        for i in indexes:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(data["text"][i].strip()), i))
            text += data["text"][i].strip()
        found = scan_text(text, categories=categories, allowlist=allowlist)
        findings += found
        for f in found:
            for start, end, i in spans:
                if start < f.end and end > f.start:
                    boxes.append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))

    draw = ImageDraw.Draw(image)
    for left, top, width, height in boxes:
        x0, y0 = left / scale - padding, top / scale - padding
        x1, y1 = (left + width) / scale + padding, (top + height) / scale + padding
        draw.rectangle([max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1)], fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), findings, len(boxes), image.size


@register_node("redact_image")
class RedactImageNode(NodeDefinition[RedactImageConfig]):
    category = "privacy"
    label = "Redact Image"
    description = "Covers card numbers, Aadhaar, PAN, keys, emails, phones, and names in a screenshot or photo with black boxes, so it's safe to share."
    icon = "eye-off"
    config_schema = RedactImageConfig
    output_schema = RedactImageResult
    queue = "ocr"

    async def execute(self, context: NodeContext, config: RedactImageConfig) -> NodeResult:
        import base64

        from flowforge_engine.files import IMAGE_TYPES, FileNotAvailable
        from flowforge_engine.nodes.documents import _open_file

        try:
            stored = await _open_file(context, config.image, IMAGE_TYPES)
        except FileNotAvailable as exc:
            return NodeResult.fail(str(exc))
        try:
            png, findings, hidden, (width, height) = await asyncio.to_thread(
                redact_image, stored.path, _categories(config.include_personal_data),
                context.services.privacy.allowlist, config.language, config.padding,
            )
        except (OSError, ValueError, RuntimeError) as exc:  # unreadable image, OCR timeout
            return NodeResult.fail(f"Couldn't redact '{stored.filename}': {exc}")
        stem = stored.filename.rsplit(".", 1)[0] or "image"
        return NodeResult.ok(
            file={"filename": f"{stem}-redacted.png", "content": base64.b64encode(png).decode("ascii"),
                  "encoding": "base64", "content_type": "image/png"},
            count=len(findings), hidden_words=hidden, summary=describe(findings) or "nothing sensitive",
            by_type=summarize(findings)["by_type"], width=width, height=height,
        )
