from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import User
from benchlog.templating import templates

router = APIRouter(prefix="/audit")

PAGE_SIZE = 100


@router.get("")
async def audit_page(
    request: Request,
    domain: str = Query("", description="Filter by domain prefix (auth, account, admin)"),
    action: list[str] = Query(default_factory=list),
    page: int = Query(1, ge=1),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    prefix = domain.strip().lower() if domain else None

    # Drop unknown actions — the UI only ever offers values from the catalog,
    # so unknowns are either stale bookmarks or tampered URLs. Silently
    # discarding keeps the page useful without leaking what's valid.
    selected_actions = [a for a in action if a in audit.ALL_ACTIONS]

    events = await audit.list_events(
        db,
        action_prefix=prefix,
        actions=selected_actions or None,
        limit=PAGE_SIZE + 1,
        offset=(page - 1) * PAGE_SIZE,
    )
    has_next = len(events) > PAGE_SIZE
    events = events[:PAGE_SIZE]

    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "user": admin,
            "events": events,
            "domain": prefix or "",
            "actions_by_domain": audit.ACTIONS_BY_DOMAIN,
            "selected_actions": set(selected_actions),
            "page": page,
            "has_next": has_next,
            "has_prev": page > 1,
        },
    )
