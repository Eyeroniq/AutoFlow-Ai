"""Create the demo user and ready-to-run pipelines. Idempotent — safe to run repeatedly.

    python -m app.db.seed

- "Demo: Summarize and email" is Input -> Gemini -> Gmail -> Output on the real providers:
  it runs once GEMINI_API_KEY and SMTP_USER/SMTP_PASSWORD are set in .env. The email goes to
  SMTP_USER itself (or you@example.com if that isn't set; change the `recipient` variable).
- "Demo: Scanned invoice to entities" is Input(File) -> OCR -> Summarize -> Entity
  Extraction -> Output. Its file input defaults to samples/scanned-invoice.pdf (an
  image-only scan), which the seed stores as one of the demo user's uploads. OCR runs on
  the worker-ocr queue, the two LLM nodes on worker-llm (Gemini; needs GEMINI_API_KEY).
"""

import asyncio
import hashlib
import logging
from typing import Any

from flowforge_engine import WorkflowGraph
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, engine
from app.models.enums import WorkflowStatus
from app.models.file import UploadedFile
from app.models.user import User
from app.models.workflow import Workflow
from app.services.files import file_path, import_file
from app.services.workflows import replace_graph

DEMO_EMAIL = "demo@flowforge.ai"
DEMO_PASSWORD = "demo1234"
DEMO_FULL_NAME = "Demo User"
DEMO_PIPELINE = "Demo: Summarize and email"
DOCUMENT_PIPELINE = "Demo: Scanned invoice to entities"
SAMPLE_SCAN = "scanned-invoice.pdf"

logger = logging.getLogger("app.seed")


def demo_graph(recipient: str) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "input", "type": "input", "label": "Topic", "position": {"x": 0, "y": 80},
                "description": "What to write about",
                "config": {"name": "topic", "input_type": "text", "default": "the history of workflow automation"},
            },
            {
                "id": "gemini", "type": "gemini", "label": "Summarize", "position": {"x": 320, "y": 80},
                "description": "Three-sentence summary with Gemini",
                "config": {
                    "provider": "gemini",
                    "system_prompt": "You are a concise technical writer.",
                    "user_prompt": "Write a three-sentence summary of {{input.topic}}.",
                    "temperature": 0.4,
                    "max_tokens": 2048,
                    # Tokens appear on the node in the editor as Gemini writes them.
                    "stream": True,
                },
            },
            {
                "id": "gmail", "type": "gmail", "label": "Email summary", "position": {"x": 640, "y": 80},
                "description": "Sends the summary",
                "config": {
                    "auth": "gmail",
                    "to": "{{vars.recipient}}",
                    "subject": "FlowForge summary: {{input.topic}}",
                    "body": "{{gemini.response}}",
                },
            },
            {
                "id": "output", "type": "output", "label": "Result", "position": {"x": 960, "y": 80},
                "config": {"name": "result", "value": {"summary": "{{gemini.response}}", "email": "{{gmail.message_id}}"}},
            },
        ],
        "edges": [
            {"source": "input", "target": "gemini"},
            {"source": "gemini", "target": "gmail"},
            {"source": "gmail", "target": "output"},
        ],
        "variables": [{"key": "recipient", "value": recipient, "type": "workflow"}],
    }


def document_graph(sample_file_id: str | None, fallback: list[str] | None = None) -> dict[str, Any]:
    """Input(File) -> OCR -> Summarize -> Entity Extraction -> Output.

    `fallback`: providers the LLM nodes try when Gemini fails (free-tier 429s and "high
    demand" 503s happen); only ones the server has keys for, or validation would fail.
    """
    fallback = fallback or []
    return {
        "nodes": [
            {
                "id": "input", "type": "input", "label": "Document", "position": {"x": 0, "y": 80},
                "description": "A PDF or image to read. Defaults to the sample scanned invoice.",
                "config": {"name": "document", "input_type": "file", "default": sample_file_id},
            },
            {
                "id": "ocr", "type": "ocr", "label": "OCR", "position": {"x": 320, "y": 80},
                "description": "Tesseract, 300 dpi (runs on worker-ocr)",
                "config": {"file": "{{input.document}}", "language": "eng", "dpi": 300},
            },
            {
                "id": "summarize", "type": "summarize", "label": "Summarize", "position": {"x": 640, "y": 80},
                "description": "Executive summary with Gemini (runs on worker-llm)",
                "config": {
                    "text": "{{ocr.text}}", "provider": "gemini", "fallback": fallback,
                    "length": "medium", "style": "executive",
                },
            },
            {
                "id": "entities", "type": "extract_entities", "label": "Entities", "position": {"x": 960, "y": 80},
                "description": "People, organizations, dates, amounts, and invoice numbers as JSON",
                "config": {
                    "text": "{{ocr.text}}",
                    "provider": "gemini",
                    "fallback": fallback,
                    "custom_types": [
                        "invoice_number: the invoice or reference number",
                        "purchase_order: the customer's purchase order number",
                    ],
                },
            },
            {
                "id": "output", "type": "output", "label": "Result", "position": {"x": 1280, "y": 80},
                "config": {
                    "name": "result",
                    "value": {
                        "summary": "{{summarize.summary}}",
                        "entities": "{{entities.entities}}",
                        "pages": "{{ocr.page_count}}",
                        "ocr_confidence": "{{ocr.mean_confidence}}",
                    },
                },
            },
        ],
        "edges": [
            {"source": "input", "target": "ocr"},
            {"source": "ocr", "target": "summarize"},
            {"source": "summarize", "target": "entities"},
            {"source": "entities", "target": "output"},
        ],
        "variables": [],
    }


async def sample_upload(session: Any, user: User) -> UploadedFile | None:
    """The sample scan as one of the user's uploads (reused if it's already there)."""
    source = settings.samples_dir / SAMPLE_SCAN
    if not source.is_file():
        logger.warning("sample scan not found; the document pipeline has no default file", extra={"path": str(source)})
        return None
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    existing = await session.scalar(
        select(UploadedFile).where(UploadedFile.owner_id == user.id, UploadedFile.sha256 == digest)
    )
    if existing is not None and file_path(existing).is_file():
        return existing
    record = await import_file(session, user.id, source)
    logger.info("sample scan stored", extra={"file_id": str(record.id)})
    return record


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            user = User(email=DEMO_EMAIL, hashed_password=hash_password(DEMO_PASSWORD), full_name=DEMO_FULL_NAME)
            session.add(user)
            await session.flush()
            logger.info("demo user created", extra={"email": DEMO_EMAIL})
        else:
            logger.info("demo user already exists", extra={"email": DEMO_EMAIL})

        existing = await session.scalar(
            select(Workflow).where(Workflow.owner_id == user.id, Workflow.name == DEMO_PIPELINE)
        )
        if existing is None:
            workflow = Workflow(
                name=DEMO_PIPELINE,
                description="Input -> Gemini -> Gmail -> Output: summarizes a topic and emails it.",
                owner_id=user.id,
                status=WorkflowStatus.ACTIVE,
                graph_json=WorkflowGraph().model_dump(mode="json"),
            )
            session.add(workflow)
            await session.flush()
            recipient = settings.SMTP_USER or "you@example.com"
            await replace_graph(session, workflow, WorkflowGraph.model_validate(demo_graph(recipient)))
            workflow.version = 1
            logger.info("demo pipeline created", extra={"workflow": DEMO_PIPELINE})
        else:
            logger.info("demo pipeline already exists", extra={"workflow": DEMO_PIPELINE})

        sample = await sample_upload(session, user)
        existing = await session.scalar(
            select(Workflow).where(Workflow.owner_id == user.id, Workflow.name == DOCUMENT_PIPELINE)
        )
        if existing is None:
            workflow = Workflow(
                name=DOCUMENT_PIPELINE,
                description="Input(File) -> OCR -> Summarize -> Entity Extraction -> Output, on a scanned invoice.",
                owner_id=user.id,
                status=WorkflowStatus.ACTIVE,
                graph_json=WorkflowGraph().model_dump(mode="json"),
            )
            session.add(workflow)
            await session.flush()
            fallback = ["groq"] if settings.GROQ_API_KEY else []
            graph = document_graph(str(sample.id) if sample else None, fallback)
            await replace_graph(session, workflow, WorkflowGraph.model_validate(graph))
            workflow.version = 1
            logger.info("document pipeline created", extra={"workflow": DOCUMENT_PIPELINE})
        else:
            logger.info("document pipeline already exists", extra={"workflow": DOCUMENT_PIPELINE})
        await session.commit()


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    setup_logging(settings.LOG_LEVEL)
    asyncio.run(main())
