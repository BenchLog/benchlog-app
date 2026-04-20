"""add projects search vector (GENERATED tsvector + GIN index)

Revision ID: c2f715a3d84e
Revises: b31e00500c74
Create Date: 2026-04-19 23:30:00.000000

Adds a `search_vector` column to `projects` as a STORED generated column
computed from `to_tsvector('english', title || ' ' || description)`, plus a
GIN index to back the full-text search. Alembic's --autogenerate doesn't
handle GENERATED columns cleanly, so this is hand-written. The SQLAlchemy
model mirrors this via `Computed(..., persisted=True)` so `Base.metadata.
create_all` (used by the test bootstrap) produces an equivalent schema.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c2f715a3d84e'
down_revision: Union[str, None] = 'b31e00500c74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )
    op.create_index(
        "ix_projects_search_vector",
        "projects",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_projects_search_vector", table_name="projects")
    op.drop_column("projects", "search_vector")
