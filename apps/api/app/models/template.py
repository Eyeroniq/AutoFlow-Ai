from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Template(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "templates"

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), index=True)
    # Full starter React Flow graph for this template.
    graph_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
