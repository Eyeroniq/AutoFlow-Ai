from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)


def parse_addresses(value: str | list[str] | None) -> list[str]:
    """Accept a list or a comma/semicolon-separated string."""
    if value is None:
        return []
    items = value if isinstance(value, list) else value.replace(";", ",").split(",")
    return [item.strip() for item in items if item and item.strip()]


def invalid_addresses(addresses: list[str]) -> list[str]:
    bad = []
    for address in addresses:
        try:
            _email_adapter.validate_python(address)
        except ValidationError:
            bad.append(address)
    return bad


class GmailConfig(NodeConfig):
    to: str | list[str] = Field(description="Recipient(s): a list, or comma-separated.")
    cc: str | list[str] | None = None
    subject: str = Field(min_length=1)
    body: str


class GmailResult(BaseModel):
    message_id: str
    status: str
    provider: str
    to: list[str]
    cc: list[str]
    subject: str
    sent_at: str
    mock: bool


@register_node("gmail")
class GmailNode(NodeDefinition[GmailConfig]):
    category = "integration"
    label = "Gmail"
    description = "Sends an email. Uses the mock email provider until Gmail OAuth is wired up."
    icon = "mail"
    config_schema = GmailConfig
    output_schema = GmailResult

    async def execute(self, context: NodeContext, config: GmailConfig) -> NodeResult:
        to, cc = parse_addresses(config.to), parse_addresses(config.cc)
        if not to:
            return NodeResult.fail("At least one recipient is required")
        bad = invalid_addresses(to + cc)
        if bad:
            return NodeResult.fail(f"Invalid email address(es): {', '.join(bad)}")

        receipt = await context.services.email("gmail").send_email(
            to=to, cc=cc, subject=config.subject, body=config.body
        )
        return NodeResult.ok(**receipt)
