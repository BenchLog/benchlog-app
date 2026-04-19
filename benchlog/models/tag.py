import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, new_uuid


class Tag(Base):
    """A flat, shared tag used for descriptive metadata across all users.

    Tags auto-create on first use — there's no separate tag management UI.
    The slug is the identity and the display: `#woodworking`, `#3d-printing`.
    Kept lowercase + hyphenated so `#Woodworking` and `#woodworking` reuse
    the same row (first-write casing wouldn't otherwise match).
    """

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    projects: Mapped[list["Project"]] = relationship(  # noqa: F821
        secondary="project_tags", back_populates="tags"
    )


class ProjectTag(Base):
    __tablename__ = "project_tags"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
