"""rename project_updates to journal_entries; add slug + is_pinned

Revision ID: c8f9d1e2a3b4
Revises: b7e9102d4c5a
Create Date: 2026-04-21 14:00:00.000000

Pre-launch rename: the "Updates" feature becomes "Journal" to match the
app's tagline. Table `project_updates` → `journal_entries`; the
`ix_project_updates_project_id` index is renamed along with it. Two new
columns land in the same revision:

- `slug` (nullable) — per-project-unique, used to deep-link titled
  entries at `/u/{user}/{slug}/journal/{entry_slug}`. NULL for untitled
  entries (which stay inline-only). Postgres treats NULLs as distinct
  in a UNIQUE constraint, so the `(project_id, slug)` uniqueness holds
  without blocking multiple untitled entries per project.
- `is_pinned` — NOT NULL bool, default False. Drives the pinned-first
  ordering on the journal feed.

No production users yet; downgrade is provided for the Alembic
round-trip check but the slug/is_pinned drop on downgrade is
destructive and intentional — there's no data to preserve.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c8f9d1e2a3b4"
down_revision: Union[str, None] = "b7e9102d4c5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename the table + its one existing index.
    op.rename_table("project_updates", "journal_entries")
    op.execute(
        "ALTER INDEX ix_project_updates_project_id "
        "RENAME TO ix_journal_entries_project_id"
    )

    # New columns.
    op.add_column(
        "journal_entries",
        sa.Column("slug", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column(
            "is_pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Per-project slug uniqueness. NULL-friendly: Postgres treats NULLs
    # as distinct, so multiple untitled entries per project are fine.
    op.create_unique_constraint(
        "uq_journal_entries_project_slug",
        "journal_entries",
        ["project_id", "slug"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_journal_entries_project_slug",
        "journal_entries",
        type_="unique",
    )
    op.drop_column("journal_entries", "is_pinned")
    op.drop_column("journal_entries", "slug")
    op.execute(
        "ALTER INDEX ix_journal_entries_project_id "
        "RENAME TO ix_project_updates_project_id"
    )
    op.rename_table("journal_entries", "project_updates")
