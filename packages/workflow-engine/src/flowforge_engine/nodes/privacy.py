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
