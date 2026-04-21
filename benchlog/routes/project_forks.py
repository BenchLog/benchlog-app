"""Route for forking a project.

Single entry: ``POST /u/{username}/{slug}/fork``. Caller must be signed
in, must not own the source, and the source must be public. Success
redirects to the new project's detail page (owned by the caller, with
visibility defaulted to private).

Must be registered before `routes.projects` — the literal `/fork` tail
would otherwise be swallowed by the `/u/{u}/{slug}`-prefixed routes.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.dependencies import require_user
from benchlog.models import User
from benchlog.projects import (
    ForkError,
    fork_project,
    get_project_by_username_and_slug,
)

router = APIRouter()


@router.post("/u/{username}/{slug}/fork")
async def fork_project_route(
    username: str,
    slug: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Fork a public project into the caller's namespace.

    Uses owner-scoped 404s throughout — we don't leak that a private
    project exists, and we don't distinguish "can't fork your own" from
    "no such project" beyond a 404. The new fork is PRIVATE regardless
    of source visibility and shares the source slug when free.
    """
    project = await get_project_by_username_and_slug(db, username, slug)
    # Gate everything behind 404 to avoid leaking existence or ownership:
    # missing project, private source, self-fork — all collapse to 404.
    if project is None or not project.is_public or project.user_id == user.id:
        raise HTTPException(status_code=404)

    try:
        new_project = await fork_project(db, project, user)
    except ForkError:
        # `fork_project` also enforces the self/visibility gates; reaching
        # this branch means a race between the gate and the helper. 404
        # stays consistent with the route-level gate above.
        raise HTTPException(status_code=404)

    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{new_project.slug}", status_code=302
    )
