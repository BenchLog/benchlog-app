"""add project short_description

Revision ID: c5e1f203a8b9
Revises: b737c1bef24f
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e1f203a8b9'
down_revision: Union[str, None] = 'b737c1bef24f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'projects',
        sa.Column('short_description', sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('projects', 'short_description')
