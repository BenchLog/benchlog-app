from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import User
from benchlog.site_settings import get_site_settings
from benchlog.templating import templates

router = APIRouter(prefix="/settings")


@router.get("")
async def settings_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    site = await get_site_settings(db)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"user": admin, "site": site, "error": error, "notice": notice},
    )


@router.post("/save")
async def save_settings(
    request: Request,
    site_name: str = Form("BenchLog"),
    allow_local_signup: bool = Form(False),
    require_email_verification: bool = Form(False),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    site = await get_site_settings(db)
    new_site_name = site_name.strip() or "BenchLog"
    changes = []
    if site.site_name != new_site_name:
        changes.append("site_name")
    if site.allow_local_signup != allow_local_signup:
        changes.append("allow_local_signup")
    if site.require_email_verification != require_email_verification:
        changes.append("require_email_verification")
    site.site_name = new_site_name
    site.allow_local_signup = allow_local_signup
    site.require_email_verification = require_email_verification
    if changes:
        await audit.record(
            db,
            action=audit.ADMIN_SETTINGS_UPDATED,
            request=request,
            actor=admin,
            metadata={"changed": changes},
        )
    await db.commit()
    request.session["flash_notice"] = "Site settings saved."
    return RedirectResponse("/admin/settings", status_code=302)
