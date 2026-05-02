import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from benchlog.models.base import Base, TimestampMixin, new_uuid


class UserSocialLinkType(str, enum.Enum):
    """Curated-set typing for user profile social links.

    Drives the label + Lucide icon on rendered rows. Keep an `other`
    bucket so users can record something we don't have a dedicated slot
    for yet. Project links are free-form and do not use this enum.
    """

    github = "github"
    gitlab = "gitlab"
    codeberg = "codeberg"
    forgejo = "forgejo"
    mastodon = "mastodon"
    bluesky = "bluesky"
    # Also covers X — we keep the legacy name because Lucide's icon is still
    # called `twitter` and renaming the enum would force a migration-plus-
    # rewrite for no user benefit.
    twitter = "twitter"
    website = "website"
    youtube = "youtube"
    instagram = "instagram"
    linkedin = "linkedin"
    other = "other"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def icon(self) -> str:
        """Lucide icon name suitable for `<i data-lucide="...">`."""
        return _ICONS[self]


_LABELS: dict[UserSocialLinkType, str] = {
    UserSocialLinkType.github: "GitHub",
    UserSocialLinkType.gitlab: "GitLab",
    UserSocialLinkType.codeberg: "Codeberg",
    UserSocialLinkType.forgejo: "Forgejo",
    UserSocialLinkType.mastodon: "Mastodon",
    UserSocialLinkType.bluesky: "Bluesky",
    UserSocialLinkType.twitter: "Twitter / X",
    UserSocialLinkType.website: "Website",
    UserSocialLinkType.youtube: "YouTube",
    UserSocialLinkType.instagram: "Instagram",
    UserSocialLinkType.linkedin: "LinkedIn",
    UserSocialLinkType.other: "Other",
}

# Lucide (v0.468) ships `github` + `gitlab` but not `codeberg` / `forgejo` /
# `mastodon` / `bluesky`. Codeberg + Forgejo fall back to `git-branch` as a
# generic forge glyph; Mastodon + Bluesky to `at-sign` for fediverse handles.
# Revisit if Lucide adds dedicated glyphs later.
_ICONS: dict[UserSocialLinkType, str] = {
    UserSocialLinkType.github: "github",
    UserSocialLinkType.gitlab: "gitlab",
    UserSocialLinkType.codeberg: "git-branch",
    UserSocialLinkType.forgejo: "git-branch",
    UserSocialLinkType.mastodon: "at-sign",
    UserSocialLinkType.bluesky: "at-sign",
    UserSocialLinkType.twitter: "twitter",
    UserSocialLinkType.website: "globe",
    UserSocialLinkType.youtube: "youtube",
    UserSocialLinkType.instagram: "instagram",
    UserSocialLinkType.linkedin: "linkedin",
    UserSocialLinkType.other: "link",
}


class UserSocialLink(TimestampMixin, Base):
    __tablename__ = "user_social_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    link_type: Mapped[UserSocialLinkType] = mapped_column(
        Enum(UserSocialLinkType, name="user_social_link_type"),
        default=UserSocialLinkType.other,
    )
    url: Mapped[str] = mapped_column(String(2048))
    # Reserved for manual ordering later; for now rows are appended and the
    # order_by on User.social_links pairs this with created_at for stability.
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship(back_populates="social_links")  # noqa: F821
