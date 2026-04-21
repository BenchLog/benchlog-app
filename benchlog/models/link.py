import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, new_uuid


class LinkType(str, enum.Enum):
    """Link categorisation. Drives the icon + label on rendered link rows.

    Visibility inherits the parent project — we don't split links per-link
    like we do with journal entries, since a link is essentially part of a
    project's metadata rather than standalone content.
    """

    github = "github"
    website = "website"
    video = "video"
    cloud_storage = "cloud_storage"
    store = "store"
    documentation = "documentation"
    other = "other"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def icon(self) -> str:
        """Lucide icon name suitable for `<i data-lucide="...">`."""
        return _ICONS[self]


_LABELS: dict[LinkType, str] = {
    LinkType.github: "GitHub",
    LinkType.website: "Website",
    LinkType.video: "Video",
    LinkType.cloud_storage: "Cloud storage",
    LinkType.store: "Store",
    LinkType.documentation: "Documentation",
    LinkType.other: "Other",
}

_ICONS: dict[LinkType, str] = {
    LinkType.github: "github",
    LinkType.website: "globe",
    LinkType.video: "youtube",
    LinkType.cloud_storage: "cloud",
    LinkType.store: "shopping-cart",
    LinkType.documentation: "book-open",
    LinkType.other: "link",
}


class ProjectLink(Base):
    __tablename__ = "project_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    link_type: Mapped[LinkType] = mapped_column(
        Enum(LinkType, name="link_type"), default=LinkType.other
    )
    # Manual ordering — reserved for later when we add reorder controls.
    # Current rendering falls back to created_at when sort_orders tie.
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="links")  # noqa: F821
