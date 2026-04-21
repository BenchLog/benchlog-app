"""Public user profile at `/u/{username}`.

Exposes a creator's bio, social links, and public projects. Accessible to
guests — route-level visibility is granted by `_is_public_project_view` in
`benchlog/middleware.py`, which whitelists 2-segment `/u/{username}` paths.

The profile view is intentionally symmetric between owner and guest: even
when an owner visits their own URL, they see the same set of projects
anyone else would. A small "Edit profile" affordance is the only owner-
specific element, keeping the view truthful to "this is how others see me."
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.collections import get_public_collections_for_user
from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import User
from benchlog.templating import templates
from benchlog.users import (
    get_active_user_by_username,
    get_public_projects_for_user,
)

router = APIRouter()


# Cap on projects listed — matches the "display everything, don't paginate
# yet" decision. The `/explore` route doesn't paginate either, so bring a
# pagination design when revisiting both together.
PROFILE_PROJECT_LIMIT = 50


@router.get("/u/{username}")
async def profile_page(
    username: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    profile_user = await get_active_user_by_username(db, username)
    if profile_user is None:
        raise HTTPException(status_code=404)

    public_projects = await get_public_projects_for_user(
        db, profile_user.id, limit=PROFILE_PROJECT_LIMIT
    )
    # Public collections surface on the profile under the projects grid.
    # Returned as a list of (collection, project_count) tuples.
    public_collections = await get_public_collections_for_user(
        db,
        profile_user.id,
        viewer_id=viewer.id if viewer is not None else None,
    )

    is_owner = viewer is not None and viewer.id == profile_user.id

    return templates.TemplateResponse(
        request,
        "users/profile.html",
        {
            # `user` is the viewer — nav + owner-menu expect this name.
            "user": viewer,
            "profile_user": profile_user,
            "social_links": profile_user.social_links,
            "public_projects": public_projects,
            "public_collections": public_collections,
            "is_owner": is_owner,
        },
    )
