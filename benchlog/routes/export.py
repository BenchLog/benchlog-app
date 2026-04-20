"""Whole-project export route.

A single endpoint that returns `{slug}.zip` containing project metadata
(JSON), a human-readable README, and every current file version. Public
projects are exportable by anyone (guests see only public updates);
private projects are owner-only, matching the web UI's visibility rules.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.export import build_project_export
from benchlog.models import User
from benchlog.projects import get_project_by_username_and_slug
from benchlog.storage import get_storage

router = APIRouter()


@router.get("/u/{username}/{slug}/export")
async def export_project(
    username: str,
    slug: str,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)

    # Guests on a public project only see public updates; owner gets
    # everything (same rule as the Updates tab rendering).
    zip_bytes = await build_project_export(
        project, get_storage(), include_private_updates=is_owner
    )
    zip_name = f"{project.slug}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{zip_name}"; '
                f"filename*=UTF-8''{quote(zip_name)}"
            ),
        },
    )
