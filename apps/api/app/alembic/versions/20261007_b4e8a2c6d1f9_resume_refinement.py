"""resume refinement

- resume_refinements: one run of the multi-agent resume pipeline on one PDF (every agent's stage
  with its raw replies, the assembled result, the version of the same file).
- resume_emails: refined resumes emailed to their owner (who, when, which version).

Revision ID: b4e8a2c6d1f9
Revises: a1d7c3e9b5f2
Create Date: 2026-10-07 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4e8a2c6d1f9'
down_revision: Union[str, None] = 'a1d7c3e9b5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'resume_refinements',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('file_id', sa.UUID(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_sha256', sa.String(length=64), nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('job_description', sa.Text(), server_default='', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('stages_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('result_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_resume_refinements_owner_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_resume_refinements_file_id_files'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_resume_refinements')),
    )
    op.create_index(op.f('ix_resume_refinements_owner_id'), 'resume_refinements', ['owner_id'])
    op.create_index(op.f('ix_resume_refinements_file_sha256'), 'resume_refinements', ['file_sha256'])
    op.create_index(op.f('ix_resume_refinements_status'), 'resume_refinements', ['status'])
    op.create_table(
        'resume_emails',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('refinement_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('to_email', sa.String(length=320), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('attachments_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('message_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['refinement_id'], ['resume_refinements.id'], name=op.f('fk_resume_emails_refinement_id_resume_refinements'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_resume_emails_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_resume_emails')),
    )
    op.create_index(op.f('ix_resume_emails_refinement_id'), 'resume_emails', ['refinement_id'])
    op.create_index(op.f('ix_resume_emails_user_id'), 'resume_emails', ['user_id'])


def downgrade() -> None:
    op.drop_table('resume_emails')
    op.drop_table('resume_refinements')
