import uuid

from sqlalchemy import Boolean, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class User(TimestampMixin, Base):
    __tablename__ = "users"
    # Functional unique indexes on lower(col) enforce case-insensitive
    # uniqueness at the DB level. The app already lowercases on signup, so
    # these are belt-and-braces for any future code path that forgets.
    __table_args__ = (
        Index("ix_users_email_lower", text("lower(email)"), unique=True),
        Index("ix_users_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(256))
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str | None] = mapped_column(String(256))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a user requests an email change. Only committed to `email` once
    # the user clicks the verification link sent to this address. Keeps the
    # verified address live for login/notifications until the new one is proven.
    pending_email: Mapped[str | None] = mapped_column(String(256), default=None)
    is_site_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on password change, admin reset, disable, demote, delete. Sessions
    # compare against this value so signed-cookie sessions can be invalidated.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Free-form markdown bio shown on the public profile page. Null when the
    # user hasn't written one yet — the profile template hides the section
    # rather than rendering an empty block.
    bio: Mapped[str | None] = mapped_column(Text, default=None)

    # lazy='raise_on_sql' — templates must not trigger implicit IO. Routes that
    # need these collections must eager-load via selectinload.
    oidc_identities: Mapped[list["OIDCIdentity"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    email_tokens: Mapped[list["EmailToken"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    passkeys: Mapped[list["WebAuthnCredential"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    projects: Mapped[list["Project"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    collections: Mapped[list["Collection"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    social_links: Mapped[list["UserSocialLink"]] = relationship(  # noqa: F821
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="(UserSocialLink.sort_order, UserSocialLink.created_at)",
        lazy="raise_on_sql",
    )
