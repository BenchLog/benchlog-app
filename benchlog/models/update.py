import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectUpdate(TimestampMixin, Base):
    __tablename__ = "project_updates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)

    project: Mapped["Project"] = relationship(back_populates="updates")  # noqa: F821
