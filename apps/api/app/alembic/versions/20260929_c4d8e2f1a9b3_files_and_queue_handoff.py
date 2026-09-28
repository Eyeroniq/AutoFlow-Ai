"""files and queue hand-off

`files` holds uploads (POST /api/files): metadata here, bytes on the shared upload volume.
workflow_executions gains `segment` and `handoff_at` for runs that move between queues
(a worker runs the nodes of its queue, then hands the run to the next queue's workers);
node_executions records which queue and worker ran each node.

Revision ID: c4d8e2f1a9b3
Revises: b6e1c4a9d2f7
Create Date: 2026-09-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2f1a9b3'
down_revision: Union[str, None] = 'b6e1c4a9d2f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'files',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('storage_key', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_files_owner_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_files')),
    )
    op.create_index(op.f('ix_files_owner_id'), 'files', ['owner_id'], unique=False)
    op.add_column('workflow_executions', sa.Column('segment', sa.Integer(), server_default='0', nullable=False))
    op.add_column('workflow_executions', sa.Column('handoff_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('node_executions', sa.Column('queue', sa.String(length=100), nullable=True))
    op.add_column('node_executions', sa.Column('worker_hostname', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('node_executions', 'worker_hostname')
    op.drop_column('node_executions', 'queue')
    op.drop_column('workflow_executions', 'handoff_at')
    op.drop_column('workflow_executions', 'segment')
    op.drop_index(op.f('ix_files_owner_id'), table_name='files')
    op.drop_table('files')
