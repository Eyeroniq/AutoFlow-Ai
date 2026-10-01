"""privacy settings and per-step findings

- workflows.privacy_json: the workflow's privacy settings (mask stored step data, detect
  personal data, allowlist); empty means the defaults.
- node_executions.privacy_json: counts and types of what was found in the step's input and
  output, and what the privacy guard did (never the values).

Revision ID: e7c1a4d9b2f6
Revises: d2a8c5e7f1b4
Create Date: 2026-10-04 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e7c1a4d9b2f6'
down_revision: Union[str, None] = 'd2a8c5e7f1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('privacy_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))
    op.add_column('node_executions', sa.Column('privacy_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('node_executions', 'privacy_json')
    op.drop_column('workflows', 'privacy_json')
