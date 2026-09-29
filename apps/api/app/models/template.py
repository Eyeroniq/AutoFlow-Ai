from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Template(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A ready-made pipeline users copy with "Use template". The rows are synced from the
    catalog in app.services.templates (on API start and by the seed)."""

    __tablename__ = "templates"

    # Stable id from the catalog, e.g. "morning-digest".
    slug: Mapped[str | None] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), index=True)
    # Full starter React Flow graph for this template.
    graph_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    # [{provider, label, why, optional}]: the credentials a run needs.
    requirements_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    # Triggers created (disabled) with the copy: [{type, config}].
    triggers_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
