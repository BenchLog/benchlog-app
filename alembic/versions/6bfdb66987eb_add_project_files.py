"""add project files

Revision ID: 6bfdb66987eb
Revises: 047e59d6fd67
Create Date: 2026-04-19 12:59:47.347736

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6bfdb66987eb'
down_revision: Union[str, None] = '047e59d6fd67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=False, server_default=''),
        sa.Column('filename', sa.String(length=256), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'show_in_gallery',
            sa.Boolean(),
            nullable=False,
            server_default='true',
        ),
        sa.Column('current_version_id', sa.Uuid(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['current_version_id'],
            ['file_versions.id'],
            name='fk_project_files_current_version_id',
            ondelete='SET NULL',
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'project_id', 'path', 'filename', name='uq_project_files_path_filename'
        ),
    )
    op.create_index(
        op.f('ix_project_files_project_id'),
        'project_files',
        ['project_id'],
        unique=False,
    )
    op.create_table(
        'file_versions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('file_id', sa.Uuid(), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('storage_path', sa.String(length=1024), nullable=False),
        sa.Column('original_name', sa.String(length=256), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('changelog', sa.Text(), nullable=True),
        sa.Column(
            'uploaded_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('thumbnail_path', sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(
            ['file_id'], ['project_files.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'file_id', 'version_number', name='uq_file_versions_file_version'
        ),
    )
    op.create_index(
        op.f('ix_file_versions_file_id'),
        'file_versions',
        ['file_id'],
        unique=False,
    )
    op.add_column('projects', sa.Column('cover_file_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_projects_cover_file_id',
        'projects',
        'project_files',
        ['cover_file_id'],
        ['id'],
        ondelete='SET NULL',
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint('fk_projects_cover_file_id', 'projects', type_='foreignkey')
    op.drop_column('projects', 'cover_file_id')
    op.drop_index(op.f('ix_file_versions_file_id'), table_name='file_versions')
    op.drop_table('file_versions')
    op.drop_index(op.f('ix_project_files_project_id'), table_name='project_files')
    op.drop_table('project_files')
