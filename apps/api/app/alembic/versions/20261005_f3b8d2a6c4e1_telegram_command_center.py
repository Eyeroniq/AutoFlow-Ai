"""telegram command center

- trigger_type and execution_trigger gain 'telegram' (a Telegram message trigger, and runs it
  started).
- deployments.description: what the pipeline does, which the Telegram intent router matches
  messages against (required when deploying from now on).
- deployments.side_effects: set on deploy when the graph sends or writes somewhere (email,
  chat messages, non-GET HTTP, Notion/Airtable writes, knowledge-base writes); such runs
  started from Telegram wait for a Confirm tap.

Revision ID: f3b8d2a6c4e1
Revises: e7c1a4d9b2f6
Create Date: 2026-10-05 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b8d2a6c4e1'
down_revision: Union[str, None] = 'e7c1a4d9b2f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't share a transaction with statements that use the value.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE trigger_type ADD VALUE IF NOT EXISTS 'telegram'")
        op.execute("ALTER TYPE execution_trigger ADD VALUE IF NOT EXISTS 'telegram'")
    op.add_column('deployments', sa.Column('description', sa.Text(), server_default='', nullable=False))
    op.add_column('deployments', sa.Column('side_effects', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('deployments', 'side_effects')
    op.drop_column('deployments', 'description')
    # Postgres can't drop an enum value; runs and triggers of type 'telegram' are removed so
    # nothing refers to it, and the value stays (harmless).
    op.execute("DELETE FROM workflow_triggers WHERE type = 'telegram'")
    op.execute("UPDATE workflow_executions SET trigger = 'manual' WHERE trigger = 'telegram'")
