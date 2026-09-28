"""unique credential per provider

A user has at most one stored credential and one integration row per provider, so
connecting again replaces the previous key instead of adding a second one.

Revision ID: 3f9c2d71b8e4
Revises: 7a125387cc29
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3f9c2d71b8e4'
down_revision: Union[str, None] = '7a125387cc29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(op.f('uq_credentials_user_id_provider'), 'credentials', ['user_id', 'provider'])
    op.create_unique_constraint(op.f('uq_integrations_user_id_provider'), 'integrations', ['user_id', 'provider'])


def downgrade() -> None:
    op.drop_constraint(op.f('uq_integrations_user_id_provider'), 'integrations', type_='unique')
    op.drop_constraint(op.f('uq_credentials_user_id_provider'), 'credentials', type_='unique')
