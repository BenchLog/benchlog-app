from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from benchlog.dependencies import require_admin
from benchlog.routes.admin import (
    audit,
    categories,
    oidc_providers,
    settings,
    smtp,
    users,
)

router = APIRouter(prefix="/admin")


@router.get("", dependencies=[Depends(require_admin)])
async def admin_index():
    return RedirectResponse("/admin/users", status_code=302)


router.include_router(users.router, prefix="/users")
router.include_router(oidc_providers.router)
router.include_router(smtp.router)
router.include_router(settings.router)
router.include_router(audit.router)
router.include_router(categories.router)
