import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Deployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A workflow published as an HTTP endpoint: POST /api/v1/deployments/{id}/run.

    One per workflow. It runs a snapshot of the graph taken at deploy time, so edits in the
    editor don't reach callers until the workflow is redeployed. Callers authenticate with
    the deployment's API key, which is shown once and stored only as a SHA-256 hash.
    """

    __tablename__ = "deployments"

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), unique=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The workflow's name and graph when it was (re)deployed; runs use this graph.
    name: Mapped[str] = mapped_column(String(255))
    graph_json: Mapped[dict[str, Any]]
    # workflows.version at deploy time, so the editor can tell the deployment is behind.
    workflow_version: Mapped[int] = mapped_column(Integer)
    # Bumped by every redeploy.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # SHA-256 (hex) of the API key. Keys are 256-bit random, so a fast hash is enough.
    api_key_hash: Mapped[str] = mapped_column(String(64))
    # The key's first characters ("ffk_" + 8), to tell keys apart in the UI. Not secret.
    api_key_prefix: Mapped[str] = mapped_column(String(16))
    key_created_at: Mapped[datetime]
    deployed_at: Mapped[datetime]
    # Set by undeploy (DELETE /api/deployments/{id}): the endpoint answers 404 and the key is
    # dead. The row stays for history; deploying again reactivates it with a new key.
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    # What the pipeline does, in a sentence or two: the Telegram intent router matches
    # messages against it.
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The graph sends or writes somewhere (deployments.has_side_effects); runs started from
    # Telegram wait for a Confirm tap.
    side_effects: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
