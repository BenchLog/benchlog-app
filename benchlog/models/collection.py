"""User-curated named groups of their own projects.

Collections are the user's own editorial lens — the "I want to group these
five projects together" surface that neither folksonomy tags nor curated
categories cover. Scoped per-user, with the slug unique only within the
user's own namespace (mirrors `Project.slug`).
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class Collection(TimestampMixin, Base):
    __tablename__ = "collections"
    __table_args__ = (
        # Slugs are unique within a user's namespace — two users can both
        # have a "guitars" collection without colliding. Canonical URL is
        # `/u/{username}/collections/{slug}`.
        UniqueConstraint("user_id", "slug", name="uq_collection_user_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    # Private by default — owner sees it everywhere, no one else until they
    # flip it on. Public collections surface on the owner's profile.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(  # noqa: F821
        back_populates="collections", lazy="raise_on_sql"
    )
    projects: Mapped[list["Project"]] = relationship(  # noqa: F821
        secondary="collection_projects",
        back_populates="collections",
        lazy="raise_on_sql",
    )


class CollectionProject(Base):
    __tablename__ = "collection_projects"

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
