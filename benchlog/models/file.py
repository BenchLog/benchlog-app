import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class ProjectFile(TimestampMixin, Base):
    __tablename__ = "project_files"
    __table_args__ = (
        UniqueConstraint("project_id", "path", "filename", name="uq_project_path_filename"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(1024), default="")
    filename: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    project: Mapped["Project"] = relationship(back_populates="files")  # noqa: F821
    versions: Mapped[list["FileVersion"]] = relationship(
        back_populates="file", order_by="FileVersion.version_number.desc()"
    )


class FileVersion(Base):
    __tablename__ = "file_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"))
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
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    file: Mapped["ProjectFile"] = relationship(back_populates="versions")
