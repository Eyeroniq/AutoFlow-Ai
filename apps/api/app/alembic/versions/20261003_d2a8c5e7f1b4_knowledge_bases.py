"""knowledge bases (pgvector)

Enables the `vector` extension (the Postgres image is pgvector/pgvector:pg16) and adds:
- knowledge_bases: a user's named collection, with its embedding model and chunking.
- kb_documents: each file added, with its processing status.
- kb_chunks: the text pieces and their 768-number embeddings, with an HNSW index for
  cosine-distance search.

Revision ID: d2a8c5e7f1b4
Revises: b9d4f2a6e1c7
Create Date: 2026-10-03 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd2a8c5e7f1b4'
down_revision: Union[str, None] = 'b9d4f2a6e1c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

document_status = postgresql.ENUM('pending', 'processing', 'ready', 'failed', name='document_status', create_type=False)


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    document_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        'knowledge_bases',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('embedding_provider', sa.String(length=50), nullable=False),
        sa.Column('embedding_model', sa.String(length=200), nullable=True),
        sa.Column('dimensions', sa.Integer(), server_default='768', nullable=False),
        sa.Column('chunk_size', sa.Integer(), nullable=False),
        sa.Column('chunk_overlap', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_knowledge_bases_owner_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_bases')),
        sa.UniqueConstraint('owner_id', 'name', name=op.f('uq_knowledge_bases_owner_id_name')),
    )
    op.create_index(op.f('ix_knowledge_bases_owner_id'), 'knowledge_bases', ['owner_id'], unique=False)
    op.create_table(
        'kb_documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
        sa.Column('file_id', sa.UUID(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('status', document_status, server_default='pending', nullable=False),
        sa.Column('source_type', sa.String(length=20), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=True),
        sa.Column('chunk_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('char_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_kb_documents_file_id_files'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], name=op.f('fk_kb_documents_knowledge_base_id_knowledge_bases'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_kb_documents')),
    )
    op.create_index(op.f('ix_kb_documents_knowledge_base_id'), 'kb_documents', ['knowledge_base_id'], unique=False)
    op.create_table(
        'kb_chunks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(768), nullable=False),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['kb_documents.id'], name=op.f('fk_kb_chunks_document_id_kb_documents'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], name=op.f('fk_kb_chunks_knowledge_base_id_knowledge_bases'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_kb_chunks')),
    )
    op.create_index(op.f('ix_kb_chunks_document_id'), 'kb_chunks', ['document_id'], unique=False)
    op.create_index(op.f('ix_kb_chunks_knowledge_base_id'), 'kb_chunks', ['knowledge_base_id'], unique=False)
    op.create_index(
        'ix_kb_chunks_embedding_hnsw', 'kb_chunks', ['embedding'], unique=False,
        postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    op.drop_index('ix_kb_chunks_embedding_hnsw', table_name='kb_chunks', postgresql_using='hnsw')
    op.drop_index(op.f('ix_kb_chunks_knowledge_base_id'), table_name='kb_chunks')
    op.drop_index(op.f('ix_kb_chunks_document_id'), table_name='kb_chunks')
    op.drop_table('kb_chunks')
    op.drop_index(op.f('ix_kb_documents_knowledge_base_id'), table_name='kb_documents')
    op.drop_table('kb_documents')
    op.drop_index(op.f('ix_knowledge_bases_owner_id'), table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
    document_status.drop(op.get_bind(), checkfirst=True)
    # The extension stays: dropping it needs superuser rights in some setups and is harmless to keep.
