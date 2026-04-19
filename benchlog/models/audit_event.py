import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from benchlog.models.base import Base, new_uuid


class AuditEvent(Base):
    """Append-only log of security- and admin-relevant events.

    Designed as a general site activity log: auth flows, admin actions, and
    later journal/feature events all share this table. Identify each event
    type with a dotted action string (`<domain>.<entity>.<verb>`).

    `actor_user_id` is FK with ON DELETE SET NULL so the row survives user
    deletion; `actor_label` keeps a denormalized email-at-event-time for
    display. `target_type` + `target_id` form a polymorphic pointer.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(256))

    action: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success")

    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_label: Mapped[str | None] = mapped_column(String(256))

    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))

    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
