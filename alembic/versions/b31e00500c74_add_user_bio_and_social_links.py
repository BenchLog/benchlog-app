"""add user bio + social links

Revision ID: b31e00500c74
Revises: cafe62bfd68f
Create Date: 2026-04-19 22:54:44.511852

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b31e00500c74'
down_revision: Union[str, None] = 'cafe62bfd68f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('bio', sa.Text(), nullable=True))
    op.create_table(
        'user_social_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'link_type',
            sa.Enum(
                'github', 'gitlab', 'codeberg', 'forgejo',
                'mastodon', 'bluesky', 'twitter', 'website',
                'youtube', 'instagram', 'linkedin', 'other',
                name='user_social_link_type',
            ),
            nullable=False,
        ),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_social_links_user_id'),
        'user_social_links',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_user_social_links_user_id'), table_name='user_social_links')
    op.drop_table('user_social_links')
    sa.Enum(name='user_social_link_type').drop(op.get_bind(), checkfirst=False)
    op.drop_column('users', 'bio')
