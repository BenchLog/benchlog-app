"""Curated, admin-managed category taxonomy.

Categories are a structured counterpart to tags: tags are folksonomy and
auto-create on first use; categories are hand-curated, shared across all
projects, and support n-level nesting. Slug uniqueness is scoped to the
parent so realistic labels like "Other" can live under multiple parents
without colliding.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class Category(TimestampMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        # Slugs are unique within a parent — allows "Other" under multiple
        # parents without collision, which is realistic for this tree.
        UniqueConstraint("parent_id", "slug", name="uq_category_parent_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    parent: Mapped["Category | None"] = relationship(
        remote_side="Category.id",
        back_populates="children",
        lazy="raise_on_sql",
    )
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent",
        order_by="Category.sort_order, Category.name",
        lazy="raise_on_sql",
        # Defer delete/update decisions to the DB FK (ON DELETE RESTRICT).
        # Without this, SA tries to null children.parent_id before deleting
        # a parent, which defeats the RESTRICT and lets an admin orphan a
        # subtree. With `passive_deletes="all"`, the DB decides and raises
        # IntegrityError as intended.
        passive_deletes="all",
    )
    projects: Mapped[list["Project"]] = relationship(  # noqa: F821
        secondary="project_categories",
        back_populates="categories",
        lazy="raise_on_sql",
    )


class ProjectCategory(Base):
    __tablename__ = "project_categories"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
