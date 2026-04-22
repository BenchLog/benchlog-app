"""reshape activity_event_type: drop project_became_public, add link_added + link_removed

Revision ID: e7b2f4a91c3d
Revises: d5a7c0e48f12
Create Date: 2026-04-21 16:00:00.000000

Visibility-toggle events are no longer emitted or rendered — the project
header chips already show current state, so logging every flip is noise.
At the same time, we add `link_added` / `link_removed` for ProjectLink
create/delete.

Postgres has no in-place `ALTER TYPE ... DROP VALUE`, so we rename the
old enum, create a new one with the final shape, rewrite the column, and
drop the old type. Any rows still using `project_became_public` are
purged first (pre-launch, so this is fine).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e7b2f4a91c3d"
down_revision: Union[str, None] = "d5a7c0e48f12"
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
)


def upgrade() -> None:
    # Drop any rows that reference the value we're about to remove.
    op.execute(
        "DELETE FROM activity_events WHERE event_type = 'project_became_public'"
    )
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
    # Reverse of the upgrade — reshape the enum back to the pre-revision
    # set. Rows using link_added / link_removed are purged first.
    pre = (
        "project_created",
        "project_became_public",
        "project_forked",
        "journal_entry_posted",
        "file_uploaded",
        "file_version_added",
    )
    op.execute(
        "DELETE FROM activity_events "
        "WHERE event_type IN ('link_added', 'link_removed')"
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
