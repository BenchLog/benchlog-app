"""Link sections + project links.

Each project's Links tab is an accordion of user-named sections, each
holding a list of links. Sections are scoped to a project; links live
inside exactly one section.

The schema deliberately omits a `project_id` on `ProjectLink` — the
project linkage runs through `section_id → LinkSection.project_id`.
Owner-scoping joins through that path; for typical projects (tens of
links) the indexed two-table join is microseconds in Postgres and the
removed denormalization eliminates a class of correctness bug
(cross-section drag would otherwise need to keep two FKs in sync).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class LinkSection(TimestampMixin, Base):
    __tablename__ = "link_sections"
    __table_args__ = (
        # Case-insensitive uniqueness within a project — `name_key` is
        # `lower(name)` and is what the constraint pins on. Display
        # `name` keeps the user's casing.
        UniqueConstraint(
            "project_id", "name_key", name="uq_link_sections_project_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    project: Mapped["Project"] = relationship(  # noqa: F821
        back_populates="sections", lazy="raise_on_sql"
    )
    links: Mapped[list["ProjectLink"]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="(ProjectLink.sort_order, ProjectLink.created_at)",
    )


class ProjectLink(TimestampMixin, Base):
    __tablename__ = "project_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("link_sections.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    # Plain-text only. Hard-capped at 280 chars at form + DB level — over
    # that the user is encouraged to start a journal entry instead.
    note: Mapped[str | None] = mapped_column(String(280), default=None)

    # OG / oEmbed metadata captured at link-add time. All nullable; null
    # means either never fetched or fetch failed. Hotlinked, not cached.
    og_title: Mapped[str | None] = mapped_column(String(512), default=None)
    og_description: Mapped[str | None] = mapped_column(Text, default=None)
    og_image_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    og_site_name: Mapped[str | None] = mapped_column(String(256), default=None)
    favicon_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    metadata_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    # Sort order is scoped to the containing section — re-numbered per
    # section, not per project.
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    section: Mapped["LinkSection"] = relationship(back_populates="links")
