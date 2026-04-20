import enum
import uuid

from sqlalchemy import (
    Boolean,
    Computed,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectStatus(str, enum.Enum):
    idea = "idea"
    in_progress = "in_progress"
    completed = "completed"
    archived = "archived"


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Slugs are unique per user — so two users can both have "desk-lamp".
        # The canonical URL (`/u/{username}/{slug}`) namespaces them.
        UniqueConstraint("user_id", "slug", name="uq_projects_user_slug"),
        # GIN index backing the full-text search over title + description.
        # Paired with the `search_vector` generated column below.
        Index("ix_projects_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(256))
    slug: Mapped[str] = mapped_column(String(256), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status"), default=ProjectStatus.idea
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Visibility: private by default. Owners flip this on when they're ready
    # to share the project on /explore and via direct link.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # FK to a ProjectFile (an image one) used as the project's cover. Nullable;
    # `use_alter=True` breaks the projects <-> project_files cycle.
    cover_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "project_files.id",
            use_alter=True,
            name="fk_projects_cover_file_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    # Cover crop — four normalized floats in [0.0, 1.0] describing the visible
    # 16:9 region on `cover_file`. NULL means "no crop chosen" — render the
    # full image with `object-fit: cover` (original behaviour). Stored
    # resolution-independent so the same values survive any future thumbnail
    # regeneration. Always cleared together: either all four NULL, or all four
    # set. The cover picker's modal writes these via the /cover-crop route.
    cover_crop_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    cover_crop_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    cover_crop_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    cover_crop_height: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Postgres tsvector over title + description, maintained by the DB via a
    # STORED generated column (`GENERATED ALWAYS AS ... STORED`). Matched by a
    # GIN index declared in `__table_args__`. Nullable defensively — coalesce
    # in the expression means we always get a value, but NULL keeps schema
    # evolution easy. Python doesn't write to it; use plainto_tsquery('english',
    # ...) in queries. See benchlog.routes.projects._apply_search_query.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
        nullable=True,
    )

    user: Mapped["User"] = relationship(back_populates="projects")  # noqa: F821
    tags: Mapped[list["Tag"]] = relationship(  # noqa: F821
        secondary="project_tags", back_populates="projects", lazy="raise_on_sql"
    )
    updates: Mapped[list["ProjectUpdate"]] = relationship(  # noqa: F821
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="ProjectUpdate.created_at.desc()",
    )
    links: Mapped[list["ProjectLink"]] = relationship(  # noqa: F821
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="(ProjectLink.sort_order, ProjectLink.created_at)",
    )
    files: Mapped[list["ProjectFile"]] = relationship(  # noqa: F821
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="(ProjectFile.path, ProjectFile.filename)",
        foreign_keys="ProjectFile.project_id",
    )
    cover_file: Mapped["ProjectFile | None"] = relationship(  # noqa: F821
        foreign_keys=[cover_file_id],
        post_update=True,
        lazy="raise_on_sql",
    )
