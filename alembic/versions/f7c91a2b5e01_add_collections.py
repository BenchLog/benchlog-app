"""add collections and collection_projects

Revision ID: f7c91a2b5e01
Revises: e4f5a6b7c8d9
Create Date: 2026-04-19 16:00:00.000000

User-curated named groups of their own projects. Slug unique per user
(mirrors projects). Join table cascades from both sides — deleting a
project removes its membership; deleting a collection removes the
membership entries but leaves the projects intact.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f7c91a2b5e01"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slug", name="uq_collection_user_slug"),
    )
    op.create_index(
        op.f("ix_collections_user_id"),
        "collections",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "collection_projects",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("collection_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("collection_projects")
    op.drop_index(op.f("ix_collections_user_id"), table_name="collections")
    op.drop_table("collections")
