"""add activity_events

Revision ID: d5a7c0e48f12
Revises: c8f9d1e2a3b4
Create Date: 2026-04-21 15:00:00.000000

System-generated activity events that feed the per-project tab,
per-profile "Recent activity" section, and the /explore/activity
firehose. Six event types in v1, extensible via ALTER TYPE later.

Separate from `audit_events` — this one is user-visible, project-scoped,
and cascades with both the actor and the project so a deletion wipes
its trail.

Indexed on `(project_id, created_at)` and `(actor_id, created_at)` so
the two hot reads (project feed, profile feed) stay fast. The global
firehose scans the newest-first tail of the table and doesn't need its
own index for v1 traffic volumes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d5a7c0e48f12"
down_revision: Union[str, None] = "c8f9d1e2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "project_created",
                "project_became_public",
                "project_forked",
                "journal_entry_posted",
                "file_uploaded",
                "file_version_added",
                name="activity_event_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_activity_events_project_created",
        "activity_events",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_activity_events_actor_created",
        "activity_events",
        ["actor_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_activity_events_actor_created", table_name="activity_events"
    )
    op.drop_index(
        "ix_activity_events_project_created", table_name="activity_events"
    )
    op.drop_table("activity_events")
    sa.Enum(name="activity_event_type").drop(op.get_bind(), checkfirst=False)
