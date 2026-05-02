"""add gps columns

Revision ID: a3a4d5e6f7b8
Revises: c5e1f203a8b9
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3a4d5e6f7b8'
down_revision: Union[str, None] = 'c5e1f203a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # has_gps: True when the version's bytes carry a GPSInfo IFD. False
    # when checked-and-clean. NULL when the file isn't an image (we never
    # ran the check). The Files tab renders a warning chip only when True.
    op.add_column(
        'file_versions',
        sa.Column('has_gps', sa.Boolean, nullable=True),
    )

    # is_quarantined: True when the version exists on disk but the owner
    # hasn't yet decided whether to strip GPS or publish as-is. The
    # ProjectFile's current_version_id stays unchanged while quarantined,
    # so existing visibility queries (which all filter on
    # current_version_id IS NOT NULL) hide it everywhere except the
    # owner's pending-review surface.
    op.add_column(
        'file_versions',
        sa.Column(
            'is_quarantined',
            sa.Boolean,
            nullable=False,
            server_default='false',
        ),
    )


def downgrade() -> None:
    op.drop_column('file_versions', 'is_quarantined')
    op.drop_column('file_versions', 'has_gps')
