"""add project_updates

Revision ID: dbe14c975771
Revises: 51cd0ae3a387
Create Date: 2026-04-19 11:14:42.933492

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dbe14c975771'
down_revision: Union[str, None] = '51cd0ae3a387'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_updates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_project_updates_project_id'), 'project_updates', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_project_updates_project_id'), table_name='project_updates')
    op.drop_table('project_updates')
