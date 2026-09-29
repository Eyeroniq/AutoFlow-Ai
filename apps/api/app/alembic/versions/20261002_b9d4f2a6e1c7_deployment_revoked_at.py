"""deployment revoked_at (undeploy)

- deployments.revoked_at: when the deployment was undeployed. A revoked deployment keeps
  its row (history, the executions it ran) but its endpoint answers 404.

Revision ID: b9d4f2a6e1c7
Revises: a7c3e9f15b2d
Create Date: 2026-10-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d4f2a6e1c7'
down_revision: Union[str, None] = 'a7c3e9f15b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('deployments', sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('deployments', 'revoked_at')
