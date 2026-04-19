"""Data-access helpers for ProjectLink + URL validation."""

import uuid
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import LinkType, ProjectLink


async def get_link_by_id(
    db: AsyncSession, project_id: uuid.UUID, link_id: uuid.UUID
) -> ProjectLink | None:
    """Scoped lookup — a crafted link id can't slip out from under its project."""
    result = await db.execute(
        select(ProjectLink).where(
            ProjectLink.id == link_id,
            ProjectLink.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def next_sort_order(db: AsyncSession, project_id: uuid.UUID) -> int:
    """One past the largest `sort_order` on the project — so new links
    drop in at the bottom of any existing arrangement. Returns 0 for an
    empty project."""
    result = await db.execute(
        select(func.max(ProjectLink.sort_order)).where(
            ProjectLink.project_id == project_id
        )
    )
    current_max = result.scalar_one_or_none()
    return 0 if current_max is None else current_max + 1


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
        # Web URLs must include a host; `https://` alone is meaningless.
        if scheme in _WEB_SCHEMES and not parsed.netloc:
            return ""
        return candidate
    # No scheme supplied — treat as a web URL and prepend https://.
    candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return ""
    return candidate


def parse_link_type(raw: str | None) -> LinkType:
    """Coerce a submitted string to a LinkType, defaulting to `other`."""
    if not raw:
        return LinkType.other
    try:
        return LinkType(raw)
    except ValueError:
        return LinkType.other
