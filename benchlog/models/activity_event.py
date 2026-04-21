"""System-generated events that feed the Activity views.

Distinct from `AuditEvent` (security/admin log, polymorphic target,
actor nullable on delete): this table is user-visible, project-scoped,
and cascades with both the actor and the project so a deletion naturally
takes its trail with it.

Six event types in v1 — each one is fired from a single call site. See
`benchlog/activity.py` for the write helpers and the three list helpers
that back the project/profile/explore feeds.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, new_uuid


class ActivityEventType(str, enum.Enum):
    project_created = "project_created"
    project_became_public = "project_became_public"
    project_forked = "project_forked"
    journal_entry_posted = "journal_entry_posted"
    file_uploaded = "file_uploaded"
    file_version_added = "file_version_added"


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        # Per-project feed (project detail page) and per-user feed (profile
        # + explore firehose filtered by actor) both scan by timestamp desc.
        Index(
            "ix_activity_events_project_created",
            "project_id",
            "created_at",
        ),
        Index(
            "ix_activity_events_actor_created",
            "actor_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    event_type: Mapped[ActivityEventType] = mapped_column(
        Enum(ActivityEventType, name="activity_event_type"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    actor: Mapped["User"] = relationship(lazy="raise_on_sql")  # noqa: F821
    project: Mapped["Project"] = relationship(lazy="raise_on_sql")  # noqa: F821
