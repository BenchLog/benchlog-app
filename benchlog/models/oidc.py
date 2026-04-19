import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class OIDCProvider(TimestampMixin, Base):
    __tablename__ = "oidc_providers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    discovery_url: Mapped[str] = mapped_column(String(512))
    client_id: Mapped[str] = mapped_column(String(256))
    client_secret: Mapped[str] = mapped_column(String(512))
    scopes: Mapped[str] = mapped_column(String(256), default="openid email profile")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_create_users: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_link_verified_email: Mapped[bool] = mapped_column(Boolean, default=False)
    # Opt-in: permit outbound OIDC requests to private/loopback/link-local IPs.
    # Required for self-hosted IdPs on a LAN; default off to keep the SSRF
    # guard active for cloud-hosted providers.
    allow_private_network: Mapped[bool] = mapped_column(Boolean, default=False)

    identities: Mapped[list["OIDCIdentity"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class OIDCIdentity(TimestampMixin, Base):
    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint("provider_id", "subject", name="uq_oidc_identity_provider_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oidc_providers.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256))

    user: Mapped["User"] = relationship(back_populates="oidc_identities")  # noqa: F821
    provider: Mapped[OIDCProvider] = relationship(back_populates="identities")
