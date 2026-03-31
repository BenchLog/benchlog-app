import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, new_uuid


class LinkType(str, enum.Enum):
    github = "github"
    website = "website"
    video = "video"
    cloud_storage = "cloud_storage"
    store = "store"
    documentation = "documentation"
    other = "other"


class ProjectLink(Base):
    __tablename__ = "project_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    link_type: Mapped[LinkType] = mapped_column(Enum(LinkType), default=LinkType.other)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="links")  # noqa: F821
