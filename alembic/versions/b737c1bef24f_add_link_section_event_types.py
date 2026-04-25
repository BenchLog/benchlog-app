"""extend activity_event_type: add link_section_(created|renamed|deleted)

Revision ID: b737c1bef24f
Revises: 55a77b7c702f
Create Date: 2026-04-24 13:00:00.000000

Three new event types fire from the section-CRUD routes. Postgres has
no in-place ALTER TYPE ADD VALUE that participates in transactions
cleanly across versions, so we follow the same rename-recreate dance
established by `e7b2f4a91c3d`.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b737c1bef24f"
down_revision: Union[str, None] = "55a77b7c702f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FINAL_VALUES = (
    "project_created",
    "project_forked",
    "journal_entry_posted",
    "file_uploaded",
    "file_version_added",
    "link_added",
    "link_removed",
    "link_section_created",
    "link_section_renamed",
    "link_section_deleted",
)


def upgrade() -> None:
    op.execute("ALTER TYPE activity_event_type RENAME TO activity_event_type_old")
    values_sql = ", ".join(f"'{v}'" for v in _FINAL_VALUES)
    op.execute(f"CREATE TYPE activity_event_type AS ENUM ({values_sql})")
    op.execute(
        "ALTER TABLE activity_events "
        "ALTER COLUMN event_type TYPE activity_event_type "
        "USING event_type::text::activity_event_type"
    )
    op.execute("DROP TYPE activity_event_type_old")


def downgrade() -> None:
    pre = (
        "project_created",
        "project_forked",
        "journal_entry_posted",
        "file_uploaded",
        "file_version_added",
        "link_added",
        "link_removed",
    )
    op.execute(
        "DELETE FROM activity_events "
        "WHERE event_type IN ("
        "'link_section_created', 'link_section_renamed', 'link_section_deleted'"
        ")"
    )
    op.execute("ALTER TYPE activity_event_type RENAME TO activity_event_type_old")
    values_sql = ", ".join(f"'{v}'" for v in pre)
    op.execute(f"CREATE TYPE activity_event_type AS ENUM ({values_sql})")
    op.execute(
        "ALTER TABLE activity_events "
        "ALTER COLUMN event_type TYPE activity_event_type "
        "USING event_type::text::activity_event_type"
    )
    op.execute("DROP TYPE activity_event_type_old")
