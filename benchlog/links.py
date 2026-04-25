"""Helpers for ProjectLink + LinkSection — URL validation, name keys,
and sort-order computation."""

import uuid
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import LinkSection, ProjectLink


# ---------- URL normalization ---------- #


_BLOCKED_SCHEMES = frozenset({"javascript", "data", "vbscript"})
_WEB_SCHEMES = frozenset({"http", "https"})


def normalize_url(raw: str) -> str:
    """Trim + default to https:// when no scheme is supplied.

    Accepts any scheme (http, https, mailto, ssh, ftp, custom…) except
    the handful that would execute as script when rendered in an anchor
    (`javascript:`, `data:`, `vbscript:`). Returns empty string when
    input is unusable; the caller treats that as a validation error.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme in _BLOCKED_SCHEMES:
            return ""
        if scheme in _WEB_SCHEMES and not parsed.netloc:
            return ""
        return candidate
    candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return ""
    return candidate


# ---------- section helpers ---------- #


def section_name_key(raw: str) -> str:
    """Lowercase + trim. The on-disk uniqueness key — never user-facing."""
    return (raw or "").strip().lower()


async def find_section_by_name_key(
    db: AsyncSession, project_id: uuid.UUID, name_key: str
) -> LinkSection | None:
    """Look up a section by its case-insensitive key. Used when the link
    modal's combobox submits an existing-or-new section name — if the
    key matches, reuse; otherwise create."""
    if not name_key:
        return None
    result = await db.execute(
        select(LinkSection).where(
            LinkSection.project_id == project_id,
            LinkSection.name_key == name_key,
        )
    )
    return result.scalar_one_or_none()


async def get_section_by_id(
    db: AsyncSession, project_id: uuid.UUID, section_id: uuid.UUID
) -> LinkSection | None:
    """Scoped lookup so a crafted section_id can't jump projects."""
    result = await db.execute(
        select(LinkSection).where(
            LinkSection.id == section_id,
            LinkSection.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def next_section_sort_order(
    db: AsyncSession, project_id: uuid.UUID
) -> int:
    """One past the max — appends to the bottom of the section list."""
    result = await db.execute(
        select(func.max(LinkSection.sort_order)).where(
            LinkSection.project_id == project_id
        )
    )
    current = result.scalar_one_or_none()
    return 0 if current is None else current + 1


# ---------- link helpers ---------- #


async def get_link_by_id(
    db: AsyncSession, project_id: uuid.UUID, link_id: uuid.UUID
) -> ProjectLink | None:
    """Scoped via the section join — we still want owner-isolation even
    though links no longer carry a project_id directly."""
    result = await db.execute(
        select(ProjectLink)
        .join(LinkSection, LinkSection.id == ProjectLink.section_id)
        .where(
            ProjectLink.id == link_id,
            LinkSection.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def next_link_sort_order(
    db: AsyncSession, section_id: uuid.UUID
) -> int:
    """One past the max within the given section. Sort order is per-section."""
    result = await db.execute(
        select(func.max(ProjectLink.sort_order)).where(
            ProjectLink.section_id == section_id
        )
    )
    current = result.scalar_one_or_none()
    return 0 if current is None else current + 1
