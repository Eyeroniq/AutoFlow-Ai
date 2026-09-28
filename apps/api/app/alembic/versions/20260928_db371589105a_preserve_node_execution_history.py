"""preserve node execution history

Deleting a WorkflowNode used to cascade-delete its NodeExecution rows. node_id is now
nullable with ON DELETE SET NULL, and each row snapshots the node's type and label at
execution time so the history stays readable after the node is gone.

Revision ID: db371589105a
Revises: 68b5f4acb957
Create Date: 2026-09-28 08:57:29.848965

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db371589105a'
down_revision: Union[str, None] = '68b5f4acb957'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the snapshot columns nullable, backfill from the (still linked) nodes, then
    # tighten — adding them NOT NULL directly would fail on existing rows.
    op.add_column('node_executions', sa.Column('node_type', sa.String(length=100), nullable=True))
    op.add_column('node_executions', sa.Column('node_label', sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE node_executions AS ne
        SET node_type = wn.node_type, node_label = wn.label
        FROM workflow_nodes AS wn
        WHERE ne.node_id = wn.id
        """
    )
    op.alter_column('node_executions', 'node_type', existing_type=sa.String(length=100), nullable=False)
    op.alter_column('node_executions', 'node_label', existing_type=sa.String(length=255), nullable=False)

    op.alter_column('node_executions', 'node_id', existing_type=sa.UUID(), nullable=True)
    op.drop_constraint(op.f('fk_node_executions_node_id_workflow_nodes'), 'node_executions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_node_executions_node_id_workflow_nodes'), 'node_executions', 'workflow_nodes', ['node_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    # Lossy: rows whose node was deleted can't satisfy the old NOT NULL FK, so they go.
    op.execute("DELETE FROM node_executions WHERE node_id IS NULL")
    op.drop_constraint(op.f('fk_node_executions_node_id_workflow_nodes'), 'node_executions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_node_executions_node_id_workflow_nodes'), 'node_executions', 'workflow_nodes', ['node_id'], ['id'], ondelete='CASCADE')
    op.alter_column('node_executions', 'node_id', existing_type=sa.UUID(), nullable=False)
    op.drop_column('node_executions', 'node_label')
    op.drop_column('node_executions', 'node_type')
