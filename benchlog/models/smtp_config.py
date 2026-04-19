import uuid

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from benchlog.models.base import Base, TimestampMixin, new_uuid


class SMTPConfig(TimestampMixin, Base):
    """Singleton row: id=1 row is the active config."""

    __tablename__ = "smtp_config"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    host: Mapped[str] = mapped_column(String(256), default="")
    port: Mapped[int] = mapped_column(Integer, default=587)
    username: Mapped[str] = mapped_column(String(256), default="")
    password: Mapped[str] = mapped_column(String(512), default="")
    from_address: Mapped[str] = mapped_column(String(256), default="")
    from_name: Mapped[str] = mapped_column(String(128), default="BenchLog")
    use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    use_starttls: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
