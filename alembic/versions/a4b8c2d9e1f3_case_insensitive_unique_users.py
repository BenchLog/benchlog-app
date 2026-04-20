"""case-insensitive unique indexes on users.email and users.username

Revision ID: a4b8c2d9e1f3
Revises: 6bfdb66987eb
Create Date: 2026-04-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4b8c2d9e1f3'
down_revision: Union[str, None] = '6bfdb66987eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_users_email', table_name='users')
    op.drop_index('ix_users_username', table_name='users')
    op.create_index(
        'ix_users_email_lower', 'users', [sa.text('lower(email)')], unique=True
    )
    op.create_index(
        'ix_users_username_lower', 'users', [sa.text('lower(username)')], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_users_username_lower', table_name='users')
    op.drop_index('ix_users_email_lower', table_name='users')
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
