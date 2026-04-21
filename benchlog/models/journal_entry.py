import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class JournalEntry(TimestampMixin, Base):
    """A single journal entry on a project — markdown content, optional title.

    Deleting the parent project cascades to its entries. Listed pinned-first
    then newest-first on the project detail page; titled entries get their
    own slug + deep-linkable URL at /u/{username}/{slug}/journal/{entry_slug}.
    Untitled entries stay inline-only.

    Visibility: private by default so the owner can post raw todos and
    in-progress notes without them showing up to visitors. Effective
    visibility is the intersection with the parent project — a public
    entry on a private project is still hidden.
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        # Slug is unique within a project — two projects can both have a
        # "day-one" entry without colliding. NULL slugs (untitled entries)
        # are allowed multiple times per project; Postgres treats NULLs as
        # distinct in a UNIQUE constraint.
        UniqueConstraint(
            "project_id", "slug", name="uq_journal_entries_project_slug"
        ),
        Index("ix_journal_entries_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    title: Mapped[str | None] = mapped_column(String(256))
    slug: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    project: Mapped["Project"] = relationship(back_populates="journal_entries")  # noqa: F821
