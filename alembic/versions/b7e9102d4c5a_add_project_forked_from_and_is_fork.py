"""add project forked_from_id and is_fork

Revision ID: b7e9102d4c5a
Revises: a9c3d71f4e22
Create Date: 2026-04-21 12:00:00.000000

Fork ancestry lives on `projects` itself: `forked_from_id` is a
self-referential nullable FK with ON DELETE SET NULL so a parent
deletion doesn't nuke its forks — they become orphan forks whose
detail page renders "Forked from a deleted project". `is_fork` is
set True at fork creation and never toggled; combined with a null
`forked_from_id` it distinguishes "never forked" from "forked but
parent deleted".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7e9102d4c5a"
down_revision: Union[str, None] = "a9c3d71f4e22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("forked_from_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "is_fork",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_foreign_key(
        "fk_projects_forked_from_id",
        "projects",
        "projects",
        ["forked_from_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_projects_forked_from_id"),
        "projects",
        ["forked_from_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_forked_from_id"), table_name="projects")
    op.drop_constraint(
        "fk_projects_forked_from_id", "projects", type_="foreignkey"
    )
    op.drop_column("projects", "is_fork")
    op.drop_column("projects", "forked_from_id")
