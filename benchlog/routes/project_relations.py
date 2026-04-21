"""Routes for ProjectRelation — typed directed links between projects.

URL scheme (all owner-gated on the source project):

- `POST   /u/{username}/{slug}/relations`                     — add
- `POST   /u/{username}/{slug}/relations/{relation_id}/delete` — remove
- `GET    /u/{username}/{slug}/relations/search?q=…`           — combobox

JSON in, JSON out. `X-CSRF-Token` header for the mutating calls. Routes
must be registered **before** `routes.projects` so the literal
`/relations` segment doesn't get swallowed by `/u/{u}/{slug}` (the
project-detail catch-all).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.dependencies import require_user
from benchlog.models import RelationType, User
from benchlog.project_relations import (
    DuplicateRelationError,
    RelationError,
    add_relation,
    remove_relation,
    search_linkable_projects,
)
from benchlog.projects import get_user_project_by_slug

router = APIRouter()


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
):
    """Return the caller's project or 404.

    Mirrors `benchlog.routes.journal._require_owned_project` — URL
    username must match the signed-in user case-insensitively, and the
    slug must belong to them. Any mismatch collapses to 404.
    """
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


def _parse_relation_type(raw: str | None) -> RelationType:
    """Coerce a user-supplied type string to the enum.

    Unknown values are rejected with 400 — we don't have a "pick a
    default" fallback here because the UI is a segmented control over
    the exact enum set.
    """
    if not raw:
        raise HTTPException(status_code=400, detail="Relation type is required.")
    try:
        return RelationType(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown relation type.")


# ---------- add ---------- #


@router.post("/u/{username}/{slug}/relations")
async def add_project_relation(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new outgoing relation from the caller's project.

    Body: JSON ``{"target_id": "<uuid>", "type": "<relation_type>"}``.
    Response: ``{"id", "target_id", "target_title", "target_username",
    "target_url", "type", "type_label", "type_icon"}`` — everything
    the client needs to render the new chip without another fetch.
    """
    project = await _require_owned_project(db, user, username, slug)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    raw_target = payload.get("target_id")
    try:
        target_id = uuid.UUID(str(raw_target))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid target_id.")

    relation_type = _parse_relation_type(payload.get("type"))

    try:
        relation = await add_relation(
            db, project, target_id, relation_type, user
        )
    except DuplicateRelationError as e:
        return JSONResponse({"detail": str(e)}, status_code=409)
    except RelationError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)

    await db.commit()
    # The relation was flushed with source/target populated but we need
    # the target's owner for the display payload. `add_relation` didn't
    # eager-load target.user — fetch it via the helper's path.
    await db.refresh(relation, ["target"])
    await db.refresh(relation.target, ["user"])
    target = relation.target

    return JSONResponse(
        {
            "id": str(relation.id),
            "target_id": str(target.id),
            "target_title": target.title,
            "target_username": target.user.username,
            "target_url": f"/u/{target.user.username}/{target.slug}",
            "type": relation_type.value,
            "type_label": relation_type.label,
            "type_icon": relation_type.icon,
        },
        status_code=201,
    )


# ---------- remove ---------- #


@router.post("/u/{username}/{slug}/relations/{relation_id}/delete")
async def delete_project_relation(
    username: str,
    slug: str,
    relation_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a relation owned by the caller's project.

    POST (not DELETE) so the existing CSRF middleware covers it — our
    middleware only enforces tokens on POST. 404 for unknown /
    wrong-owner id; 204 on success.
    """
    await _require_owned_project(db, user, username, slug)

    try:
        parsed = uuid.UUID(relation_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404)

    removed = await remove_relation(db, parsed, user)
    if not removed:
        raise HTTPException(status_code=404)
    await db.commit()
    return Response(status_code=204)


# ---------- combobox search ---------- #


@router.get("/u/{username}/{slug}/relations/search")
async def search_relations(
    username: str,
    slug: str,
    q: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Typeahead lookup for the "Add relation" combobox.

    Returns a JSON list of candidate targets. Owner-only: the search
    endpoint already filters to "actor's own + public of others", but
    we still require the caller to own the source project to avoid
    exposing an arbitrary full-text search of the site via an endpoint
    that looks project-scoped (keeps rate-limit accounting simple).
    """
    project = await _require_owned_project(db, user, username, slug)

    rows = await search_linkable_projects(
        db, user, q, exclude_project_id=project.id
    )
    return {
        "results": [
            {
                "id": str(p.id),
                "title": p.title,
                "username": p.user.username,
                "url": f"/u/{p.user.username}/{p.slug}",
                "is_public": p.is_public,
            }
            for p in rows
        ]
    }
