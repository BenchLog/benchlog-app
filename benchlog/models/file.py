"""Project files — versioned blobs in a virtual folder hierarchy.

Two tables: `ProjectFile` is the logical entry (path + filename); `FileVersion`
is the stored blob. Each file points at its `current_version` so the rendered
file row doesn't need to join + sort to know its mime type or size.

Images aren't a separate model — they're `ProjectFile` rows whose current
`FileVersion.mime_type` starts with `image/`. Image-specific columns
(`width`, `height`, `thumbnail_path`) live on `FileVersion` and stay null
for non-image files.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectFile(TimestampMixin, Base):
    __tablename__ = "project_files"
    __table_args__ = (
        # Two files in the same virtual folder can't share a name.
        UniqueConstraint(
            "project_id", "path", "filename", name="uq_project_files_path_filename"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Virtual folder, '' for root. Forward-slash-separated like a unix path,
    # but stored without a leading slash. e.g. "models/widgets".
    path: Mapped[str] = mapped_column(String(1024), default="")
    filename: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    # Default: every image appears on the Gallery tab. Owners can hide
    # individual images to curate the gallery without deleting the file
    # (e.g., keep every test shot but only feature the hero photos).
    show_in_gallery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # FK to the latest FileVersion. Nullable because we have to insert the
    # file row before the first version exists; `use_alter=True` breaks the
    # circular project_files <-> file_versions dependency at table-create time.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "file_versions.id",
            use_alter=True,
            name="fk_project_files_current_version_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    project: Mapped["Project"] = relationship(  # noqa: F821
        back_populates="files",
        foreign_keys=[project_id],
    )
    versions: Mapped[list["FileVersion"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="FileVersion.version_number.desc()",
        foreign_keys="FileVersion.file_id",
    )
    current_version: Mapped["FileVersion | None"] = relationship(
        foreign_keys=[current_version_id],
        post_update=True,
        lazy="raise_on_sql",
    )


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (
        UniqueConstraint(
            "file_id", "version_number", name="uq_file_versions_file_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project_files.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(1024))
    original_name: Mapped[str] = mapped_column(String(256))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(128))
    checksum: Mapped[str] = mapped_column(String(64))
    changelog: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Image-only metadata; null for non-image versions.
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))

    # See alembic migration a3a4d5e6f7b8 for the semantics.
    has_gps: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    file: Mapped["ProjectFile"] = relationship(
        back_populates="versions", foreign_keys=[file_id]
    )

    @property
    def is_image(self) -> bool:
        return (self.mime_type or "").startswith("image/")
