import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectUpdate(TimestampMixin, Base):
    """A single journal entry on a project — markdown content, optional title.

    Deleting the parent project cascades to its updates. Listed newest
    first on the project detail page; each update also has a permalink
    at /u/{username}/{slug}/updates/{id}.

    Visibility: private by default so the owner can post raw todos and
    in-progress notes without them showing up to visitors. Effective
    visibility is the intersection with the parent project — a public
    update on a private project is still hidden.
    """

    __tablename__ = "project_updates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship(back_populates="updates")  # noqa: F821
