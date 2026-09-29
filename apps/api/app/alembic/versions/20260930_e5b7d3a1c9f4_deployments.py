"""deployments

`deployments` publishes a workflow as POST /api/v1/deployments/{id}/run: a snapshot of its
graph, a redeploy counter, and the SHA-256 hash (never the key itself) of the API key
callers authenticate with. One per workflow. workflow_executions.deployment_id records
which deployment started an API run; it is set to NULL if the deployment goes away.

Revision ID: e5b7d3a1c9f4
Revises: c4d8e2f1a9b3
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e5b7d3a1c9f4'
down_revision: Union[str, None] = 'c4d8e2f1a9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deployments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workflow_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('graph_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('workflow_version', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=False),
        sa.Column('api_key_prefix', sa.String(length=16), nullable=False),
        sa.Column('key_created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_deployments_owner_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_deployments_workflow_id_workflows'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments')),
        sa.UniqueConstraint('workflow_id', name=op.f('uq_deployments_workflow_id')),
    )
    op.create_index(op.f('ix_deployments_owner_id'), 'deployments', ['owner_id'], unique=False)
    op.add_column('workflow_executions', sa.Column('deployment_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_workflow_executions_deployment_id'), 'workflow_executions', ['deployment_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_workflow_executions_deployment_id_deployments'), 'workflow_executions', 'deployments',
        ['deployment_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(op.f('fk_workflow_executions_deployment_id_deployments'), 'workflow_executions', type_='foreignkey')
    op.drop_index(op.f('ix_workflow_executions_deployment_id'), table_name='workflow_executions')
    op.drop_column('workflow_executions', 'deployment_id')
    op.drop_index(op.f('ix_deployments_owner_id'), table_name='deployments')
    op.drop_table('deployments')
