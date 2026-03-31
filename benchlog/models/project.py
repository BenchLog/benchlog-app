import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectStatus(str, enum.Enum):
    idea = "idea"
    in_progress = "in_progress"
    completed = "completed"
    archived = "archived"


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(256))
    slug: Mapped[str] = mapped_column(String(256), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.idea
    )
    cover_image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", use_alter=True, name="fk_projects_cover_image_id")
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="projects")  # noqa: F821
    cover_image: Mapped["Image | None"] = relationship(foreign_keys=[cover_image_id])  # noqa: F821
    files: Mapped[list["ProjectFile"]] = relationship(back_populates="project")  # noqa: F821
    updates: Mapped[list["ProjectUpdate"]] = relationship(back_populates="project")  # noqa: F821
    bom_items: Mapped[list["BOMItem"]] = relationship(back_populates="project")  # noqa: F821
    links: Mapped[list["ProjectLink"]] = relationship(back_populates="project")  # noqa: F821
    tags: Mapped[list["Tag"]] = relationship(  # noqa: F821
        secondary="project_tags", back_populates="projects"
    )
    images: Mapped[list["Image"]] = relationship(  # noqa: F821
        back_populates="project", foreign_keys="Image.project_id"
    )
