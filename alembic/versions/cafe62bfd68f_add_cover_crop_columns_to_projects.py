"""add cover crop columns to projects

Revision ID: cafe62bfd68f
Revises: a4b8c2d9e1f3
Create Date: 2026-04-19 21:33:26.138289

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'cafe62bfd68f'
down_revision: Union[str, None] = 'a4b8c2d9e1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('cover_crop_x', sa.Float(), nullable=True))
    op.add_column('projects', sa.Column('cover_crop_y', sa.Float(), nullable=True))
    op.add_column('projects', sa.Column('cover_crop_width', sa.Float(), nullable=True))
    op.add_column('projects', sa.Column('cover_crop_height', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'cover_crop_height')
    op.drop_column('projects', 'cover_crop_width')
    op.drop_column('projects', 'cover_crop_y')
    op.drop_column('projects', 'cover_crop_x')
