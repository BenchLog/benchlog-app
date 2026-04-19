"""add is_public to project_updates

Revision ID: d4033dc688a5
Revises: dbe14c975771
Create Date: 2026-04-19 11:40:38.067718

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4033dc688a5'
down_revision: Union[str, None] = 'dbe14c975771'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'project_updates',
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('project_updates', 'is_public')
