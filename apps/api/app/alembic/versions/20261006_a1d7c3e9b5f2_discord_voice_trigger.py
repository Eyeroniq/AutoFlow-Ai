"""discord voice trigger

- execution_trigger gains 'discord_voice': runs started from a Discord voice recording that
  was approved over Telegram.

Revision ID: a1d7c3e9b5f2
Revises: f3b8d2a6c4e1
Create Date: 2026-10-06 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1d7c3e9b5f2'
down_revision: Union[str, None] = 'f3b8d2a6c4e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't share a transaction with statements that use the value.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE execution_trigger ADD VALUE IF NOT EXISTS 'discord_voice'")


def downgrade() -> None:
    # Postgres can't drop an enum value; its runs are relabelled so nothing refers to it.
    op.execute("UPDATE workflow_executions SET trigger = 'manual' WHERE trigger = 'discord_voice'")
