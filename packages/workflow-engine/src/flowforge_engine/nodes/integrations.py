from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError

from flowforge_engine.errors import ProviderError
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.providers.base import EmailAttachment, MailboxQuery, OutgoingEmail
from flowforge_engine.providers.settings import EMAIL_PROVIDER_NAMES, EmailProviderName
from flowforge_engine.registry import GuardedOutboundConfig, NodeConfig, NodeDefinition, register_node
from flowforge_engine.variables import contains_reference

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)

AUTH_DESCRIPTION = (
    "Credential to use: 'gmail' = your connected Gmail credential, or the server's SMTP_USER / "
    "SMTP_PASSWORD when you haven't connected one; 'mock' = send/read nothing."
)


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


def _auth_requirement(node: GraphNode) -> list[tuple[str, str]]:
    auth = node.config.get("auth", "gmail")
    if isinstance(auth, str) and not contains_reference(auth) and auth in EMAIL_PROVIDER_NAMES:
        return [(auth, "auth")]
    return []


class AttachmentConfig(NodeConfig):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(description="Text, or base64 when encoding is 'base64'. Usually a {{...}} reference.")
    encoding: Literal["text", "base64"] = "text"
    content_type: str | None = Field(default=None, description="MIME type; guessed from the filename if blank.")


class GmailConfig(GuardedOutboundConfig):
    auth: EmailProviderName = Field(default="gmail", description=AUTH_DESCRIPTION)
    to: str | list[str] = Field(description="Recipient(s): a list, or comma-separated.")
    cc: str | list[str] | None = None
    bcc: str | list[str] | None = None
    subject: str = Field(min_length=1)
    body: str = Field(description="Plain-text body.")
    html_body: str | None = Field(default=None, description="Optional HTML version (sent as multipart/alternative).")
    attachments: list[AttachmentConfig] = Field(default_factory=list, max_length=20)


class GmailResult(BaseModel):
    message_id: str
    status: str
    provider: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    attachments: list[str]
    sent_at: str
    mock: bool


@register_node("gmail")
class GmailNode(NodeDefinition[GmailConfig]):
    category = "integration"
    guard_fields = ('subject', 'body', 'html_body', 'attachments')
    label = "Gmail"
    description = "Sends an email through Gmail SMTP (App Password), with cc/bcc, HTML, and attachments."
    icon = "mail"
    config_schema = GmailConfig
    output_schema = GmailResult
    # Cancelling mid-send could leave it unknown whether the email went out.
    interruptible = False

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return _auth_requirement(node)

    def output_keys(self, node: GraphNode) -> set[str]:
        return {*GmailResult.model_fields, "from", "refused"}

    async def execute(self, context: NodeContext, config: GmailConfig) -> NodeResult:
        to, cc, bcc = parse_addresses(config.to), parse_addresses(config.cc), parse_addresses(config.bcc)
        if not to:
            return NodeResult.fail("At least one recipient is required")
        bad = invalid_addresses(to + cc + bcc)
        if bad:
            return NodeResult.fail(f"Invalid email address(es): {', '.join(bad)}")

        email = OutgoingEmail(
            to=to, cc=cc, bcc=bcc, subject=config.subject, body=config.body, html_body=config.html_body,
            attachments=[EmailAttachment(**a.model_dump()) for a in config.attachments],
        )
        try:
            receipt = await context.services.email(config.auth).send_email(email)
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(**receipt)


class GmailReadConfig(NodeConfig):
    auth: EmailProviderName = Field(default="gmail", description=AUTH_DESCRIPTION)
    folder: str = Field(default="INBOX", min_length=1, description="Mailbox, e.g. INBOX or \"[Gmail]/All Mail\".")
    from_address: str | None = Field(default=None, description="Only mail whose From contains this text.")
    subject: str | None = Field(default=None, description="Only mail whose Subject contains this text.")
    unread_only: bool = True
    since_days: int | None = Field(default=None, ge=1, le=3650, description="Only mail from the last N days.")
    max_results: int = Field(default=10, ge=1, le=50, description="Newest N matching messages.")
    mark_as_read: bool = Field(default=False, description="Flag the returned messages as read.")
    include_body: bool = True
    max_body_chars: int = Field(default=5000, ge=0, le=100_000)


class GmailReadResult(BaseModel):
    emails: list[dict[str, Any]]
    count: int
    folder: str
    mock: bool


@register_node("gmail_read")
class GmailReadNode(NodeDefinition[GmailReadConfig]):
    category = "integration"
    label = "Gmail Read"
    description = "Fetches recent or unread emails over IMAP; use {{<node>.emails}} downstream."
    icon = "inbox"
    config_schema = GmailReadConfig
    output_schema = GmailReadResult

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return _auth_requirement(node)

    async def execute(self, context: NodeContext, config: GmailReadConfig) -> NodeResult:
        query = MailboxQuery(**config.model_dump(exclude={"auth"}))
        try:
            mailbox = context.services.mailbox(config.auth)
            emails = await mailbox.fetch_emails(query)
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(emails=emails, count=len(emails), folder=config.folder, mock=mailbox.is_mock)
