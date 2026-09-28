"""graph node keys

workflow_nodes.node_key stores each node's id from graph_json (e.g. "gemini") so the
relational rows can be matched to the graph, unique per workflow. node_executions.node_key
snapshots it, alongside node_type/node_label, for history that outlives the node.

Revision ID: 7a125387cc29
Revises: db371589105a
Create Date: 2026-09-28 09:23:14.212775

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a125387cc29'
down_revision: Union[str, None] = 'db371589105a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get their UUID as the key (always unique); new saves use graph ids.
    op.add_column('workflow_nodes', sa.Column('node_key', sa.String(length=100), nullable=True))
    op.execute("UPDATE workflow_nodes SET node_key = id::text")
    op.alter_column('workflow_nodes', 'node_key', existing_type=sa.String(length=100), nullable=False)
    op.create_unique_constraint(op.f('uq_workflow_nodes_workflow_id_node_key'), 'workflow_nodes', ['workflow_id', 'node_key'])

    op.add_column('node_executions', sa.Column('node_key', sa.String(length=100), nullable=True))
    op.execute(
        """
        UPDATE node_executions AS ne
        SET node_key = COALESCE(
            (SELECT wn.node_key FROM workflow_nodes AS wn WHERE wn.id = ne.node_id),
            'unknown'
        )
        """
    )
    op.alter_column('node_executions', 'node_key', existing_type=sa.String(length=100), nullable=False)


def downgrade() -> None:
    op.drop_column('node_executions', 'node_key')
    op.drop_constraint(op.f('uq_workflow_nodes_workflow_id_node_key'), 'workflow_nodes', type_='unique')
    op.drop_column('workflow_nodes', 'node_key')
