"""async execution columns

workflow_executions gains what asynchronous (Celery) runs need: created_at (to expire
runs no worker ever picked up), the run's inputs and a
snapshot of the graph (so a queued run is unaffected by later edits), the queue and Celery
task id it was dispatched with, the worker that claimed it, a heartbeat used to detect a
dead worker, and when a stop was requested. node_executions gains `position` (execution
order), since rows now exist before their nodes run.

Revision ID: b6e1c4a9d2f7
Revises: 3f9c2d71b8e4
Create Date: 2026-09-28 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b6e1c4a9d2f7'
down_revision: Union[str, None] = '3f9c2d71b8e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_executions', sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.add_column('workflow_executions', sa.Column('inputs_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('workflow_executions', sa.Column('graph_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('workflow_executions', sa.Column('queue', sa.String(length=100), nullable=True))
    op.add_column('workflow_executions', sa.Column('celery_task_id', sa.String(length=255), nullable=True))
    op.add_column('workflow_executions', sa.Column('worker_hostname', sa.String(length=255), nullable=True))
    op.add_column('workflow_executions', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('workflow_executions', sa.Column('stop_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('node_executions', sa.Column('position', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('node_executions', 'position')
    op.drop_column('workflow_executions', 'stop_requested_at')
    op.drop_column('workflow_executions', 'heartbeat_at')
    op.drop_column('workflow_executions', 'worker_hostname')
    op.drop_column('workflow_executions', 'celery_task_id')
    op.drop_column('workflow_executions', 'queue')
    op.drop_column('workflow_executions', 'graph_json')
    op.drop_column('workflow_executions', 'inputs_json')
    op.drop_column('workflow_executions', 'created_at')
