"""Typed directed relations between projects.

Distinct from `ProjectLink` (external URL, lives on `project_links`) —
`ProjectRelation` is a first-class edge between two BenchLog projects.
Source owner declares; target doesn't opt in. Rendered both ways on
detail pages (outgoing on source, incoming on target).

Uniqueness is on the (source, target, relation_type) triple so a project
can simultaneously be "inspired_by X" and "depends_on X" (two rows) but
can't declare the same relation twice. A CHECK prevents self-links.
"""

import enum
import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class RelationType(str, enum.Enum):
    """Relationship category. Drives icon + label on relation chips.

    `fork_of` is intentionally included in the enum now (so the Forks
    feature's migration doesn't need to ALTER TYPE to add it) but is
    **system-generated only** — user routes reject it. Callers that
    need to create a fork relation go through `add_relation` with
    `allow_system_types=True`.
    """

    inspired_by = "inspired_by"
    related_to = "related_to"
    depends_on = "depends_on"
    fork_of = "fork_of"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def icon(self) -> str:
        """Lucide icon name suitable for `<i data-lucide="...">`."""
        return _ICONS[self]


_LABELS: dict[RelationType, str] = {
    RelationType.inspired_by: "Inspired by",
    RelationType.related_to: "Related to",
    RelationType.depends_on: "Depends on",
    RelationType.fork_of: "Fork of",
}

_ICONS: dict[RelationType, str] = {
    RelationType.inspired_by: "lightbulb",
    RelationType.related_to: "link-2",
    RelationType.depends_on: "package",
    RelationType.fork_of: "git-branch",
}

# Types the owner can pick from the "Add relation" UI. `fork_of` is
# excluded — only the Forks feature (server-side) writes that type.
USER_PICKABLE_TYPES: tuple[RelationType, ...] = (
    RelationType.inspired_by,
    RelationType.related_to,
    RelationType.depends_on,
)


class ProjectRelation(TimestampMixin, Base):
    __tablename__ = "project_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "target_id",
            "relation_type",
            name="uq_project_relation_triple",
        ),
        CheckConstraint(
            "source_id <> target_id",
            name="ck_project_relation_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[RelationType] = mapped_column(
        Enum(RelationType, name="relation_type"),
    )

    source: Mapped["Project"] = relationship(  # noqa: F821
        foreign_keys=[source_id], lazy="raise_on_sql"
    )
    target: Mapped["Project"] = relationship(  # noqa: F821
        foreign_keys=[target_id], lazy="raise_on_sql"
    )
