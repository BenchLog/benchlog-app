import uuid

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from benchlog.models.base import Base, TimestampMixin, new_uuid


class SiteSettings(TimestampMixin, Base):
    """Singleton row holding global site toggles."""

    __tablename__ = "site_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    site_name: Mapped[str] = mapped_column(String(128), default="BenchLog")
    allow_local_signup: Mapped[bool] = mapped_column(Boolean, default=True)
    require_email_verification: Mapped[bool] = mapped_column(Boolean, default=False)
