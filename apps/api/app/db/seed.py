"""Create the demo user and a ready-to-run pipeline. Idempotent — safe to run repeatedly.

    python -m app.db.seed

The pipeline is Input -> Gemini -> Gmail -> Output on the real providers: it validates and
runs from the editor once GEMINI_API_KEY and SMTP_USER/SMTP_PASSWORD are set in .env. The
email goes to SMTP_USER itself (or you@example.com if that isn't set; change the
`recipient` variable in the editor).
"""

import asyncio
import logging
from typing import Any

from flowforge_engine import WorkflowGraph
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, engine
from app.models.enums import WorkflowStatus
from app.models.user import User
from app.models.workflow import Workflow
from app.services.workflows import replace_graph

DEMO_EMAIL = "demo@flowforge.ai"
DEMO_PASSWORD = "demo1234"
DEMO_FULL_NAME = "Demo User"
DEMO_PIPELINE = "Demo: Summarize and email"

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
        await session.commit()


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    setup_logging(settings.LOG_LEVEL)
    asyncio.run(main())
