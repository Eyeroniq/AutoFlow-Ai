"""triggers, node state, and templates

- `workflow_triggers`: one schedule / email / webhook trigger per workflow, with the next
  due time (claimed with a compare-and-set), the email poller's mailbox position, and the
  consecutive-failure counter that switches a trigger off.
- `trigger_events`: every fire time or email Message-ID a trigger acted on, unique per
  trigger, so nothing fires twice.
- `node_states`: state nodes keep between runs (RSS "since last run").
- workflow_executions.trigger_id links a run to its trigger; execution_trigger gains
  'email', and deployment runs, recorded as 'api' so far, are the webhook trigger now.
- workflows gain the per-workflow limits (runs per hour, failures before disabling).
- templates gain a stable slug, the credentials they need, their triggers, an order,
  and updated_at.

Revision ID: a7c3e9f15b2d
Revises: e5b7d3a1c9f4
Create Date: 2026-10-01 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9f15b2d'
down_revision: Union[str, None] = 'e5b7d3a1c9f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

trigger_type = postgresql.ENUM('schedule', 'email', 'webhook', name='trigger_type', create_type=False)


def upgrade() -> None:
    # Usable only after this transaction commits; nothing below writes 'email'.
    op.execute("ALTER TYPE execution_trigger ADD VALUE IF NOT EXISTS 'email'")
    trigger_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'workflow_triggers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workflow_id', sa.UUID(), nullable=False),
        sa.Column('type', trigger_type, nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('config_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('state_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
        sa.Column('auto_disabled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('disabled_reason', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_workflow_triggers_workflow_id_workflows'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_workflow_triggers')),
        sa.UniqueConstraint('workflow_id', 'type', name=op.f('uq_workflow_triggers_workflow_id_type')),
    )
    op.create_index(op.f('ix_workflow_triggers_workflow_id'), 'workflow_triggers', ['workflow_id'], unique=False)
    op.create_index(op.f('ix_workflow_triggers_next_fire_at'), 'workflow_triggers', ['next_fire_at'], unique=False)

    op.add_column('workflow_executions', sa.Column('trigger_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_workflow_executions_trigger_id'), 'workflow_executions', ['trigger_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_workflow_executions_trigger_id_workflow_triggers'), 'workflow_executions', 'workflow_triggers',
        ['trigger_id'], ['id'], ondelete='SET NULL',
    )

    op.create_table(
        'trigger_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('trigger_id', sa.UUID(), nullable=False),
        sa.Column('event_key', sa.String(length=998), nullable=False),
        sa.Column('outcome', sa.String(length=32), nullable=False),
        sa.Column('execution_id', sa.UUID(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['execution_id'], ['workflow_executions.id'], name=op.f('fk_trigger_events_execution_id_workflow_executions'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['trigger_id'], ['workflow_triggers.id'], name=op.f('fk_trigger_events_trigger_id_workflow_triggers'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trigger_events')),
        sa.UniqueConstraint('trigger_id', 'event_key', name=op.f('uq_trigger_events_trigger_id_event_key')),
    )
    op.create_index(op.f('ix_trigger_events_trigger_id'), 'trigger_events', ['trigger_id'], unique=False)
    op.create_index(op.f('ix_trigger_events_execution_id'), 'trigger_events', ['execution_id'], unique=False)

    op.create_table(
        'node_states',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workflow_id', sa.UUID(), nullable=False),
        sa.Column('node_key', sa.String(length=100), nullable=False),
        sa.Column('execution_id', sa.UUID(), nullable=False),
        sa.Column('state_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['execution_id'], ['workflow_executions.id'], name=op.f('fk_node_states_execution_id_workflow_executions'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_node_states_workflow_id_workflows'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_node_states')),
    )
    op.create_index(op.f('ix_node_states_execution_id'), 'node_states', ['execution_id'], unique=False)
    op.create_index('ix_node_states_workflow_node', 'node_states', ['workflow_id', 'node_key', 'created_at'], unique=False)

    op.add_column('workflows', sa.Column('max_runs_per_hour', sa.Integer(), server_default='30', nullable=False))
    op.add_column('workflows', sa.Column('max_consecutive_failures', sa.Integer(), server_default='3', nullable=False))

    op.add_column('templates', sa.Column('slug', sa.String(length=100), nullable=True))
    op.add_column('templates', sa.Column('requirements_json', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    op.add_column('templates', sa.Column('triggers_json', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    op.add_column('templates', sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False))
    op.add_column('templates', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.create_unique_constraint(op.f('uq_templates_slug'), 'templates', ['slug'])

    # Deployment runs are the webhook trigger now.
    op.execute("UPDATE workflow_executions SET trigger = 'webhook' WHERE trigger = 'api'")


def downgrade() -> None:
    op.execute("UPDATE workflow_executions SET trigger = 'api' WHERE trigger = 'webhook' AND deployment_id IS NOT NULL")
    # Postgres can't drop an enum value; email runs become the generic 'event'.
    op.execute("UPDATE workflow_executions SET trigger = 'event' WHERE trigger = 'email'")

    op.drop_constraint(op.f('uq_templates_slug'), 'templates', type_='unique')
    op.drop_column('templates', 'updated_at')
    op.drop_column('templates', 'sort_order')
    op.drop_column('templates', 'triggers_json')
    op.drop_column('templates', 'requirements_json')
    op.drop_column('templates', 'slug')

    op.drop_column('workflows', 'max_consecutive_failures')
    op.drop_column('workflows', 'max_runs_per_hour')

    op.drop_index('ix_node_states_workflow_node', table_name='node_states')
    op.drop_index(op.f('ix_node_states_execution_id'), table_name='node_states')
    op.drop_table('node_states')

    op.drop_index(op.f('ix_trigger_events_execution_id'), table_name='trigger_events')
    op.drop_index(op.f('ix_trigger_events_trigger_id'), table_name='trigger_events')
    op.drop_table('trigger_events')

    op.drop_constraint(op.f('fk_workflow_executions_trigger_id_workflow_triggers'), 'workflow_executions', type_='foreignkey')
    op.drop_index(op.f('ix_workflow_executions_trigger_id'), table_name='workflow_executions')
    op.drop_column('workflow_executions', 'trigger_id')

    op.drop_index(op.f('ix_workflow_triggers_next_fire_at'), table_name='workflow_triggers')
    op.drop_index(op.f('ix_workflow_triggers_workflow_id'), table_name='workflow_triggers')
    op.drop_table('workflow_triggers')
    trigger_type.drop(op.get_bind(), checkfirst=True)
