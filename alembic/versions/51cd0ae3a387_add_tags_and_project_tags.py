"""add tags and project_tags

Revision ID: 51cd0ae3a387
Revises: 6f487796c5de
Create Date: 2026-04-19 08:42:46.366021

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '51cd0ae3a387'
down_revision: Union[str, None] = '6f487796c5de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tags',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tags_slug'), 'tags', ['slug'], unique=True)
    op.create_table(
        'project_tags',
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('tag_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'tag_id'),
    )


def downgrade() -> None:
    op.drop_table('project_tags')
    op.drop_index(op.f('ix_tags_slug'), table_name='tags')
    op.drop_table('tags')
