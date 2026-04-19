"""add project_links

Revision ID: 047e59d6fd67
Revises: d4033dc688a5
Create Date: 2026-04-19 11:53:19.167699

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '047e59d6fd67'
down_revision: Union[str, None] = 'd4033dc688a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column(
            'link_type',
            sa.Enum(
                'github', 'website', 'video', 'cloud_storage',
                'store', 'documentation', 'other',
                name='link_type',
            ),
            nullable=False,
        ),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_project_links_project_id'), 'project_links', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_project_links_project_id'), table_name='project_links')
    op.drop_table('project_links')
    sa.Enum(name='link_type').drop(op.get_bind(), checkfirst=False)
