from aiosmtplib.errors import SMTPException
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import SMTPConfig, User
from benchlog.email import get_smtp_config, send_test_email
from benchlog.templating import templates

router = APIRouter(prefix="/smtp")


@router.get("")
async def smtp_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    config = await get_smtp_config(db)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/smtp.html",
        {"user": admin, "config": config, "error": error, "notice": notice},
    )


@router.post("/save")
async def save_smtp(
    request: Request,
    host: str = Form(""),
    port: int = Form(587),
    username: str = Form(""),
    password: str = Form(""),
    from_address: str = Form(""),
    from_name: str = Form("BenchLog"),
    use_tls: bool = Form(False),
    use_starttls: bool = Form(False),
    enabled: bool = Form(False),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    config = await get_smtp_config(db)
    if config is None:
        config = SMTPConfig()
        db.add(config)
    config.host = host.strip()
    config.port = port
    config.username = username.strip()
    if password.strip():
        config.password = password
    config.from_address = from_address.strip()
    config.from_name = from_name.strip() or "BenchLog"
    config.use_tls = use_tls
    config.use_starttls = use_starttls
    config.enabled = enabled
    await audit.record(
        db,
        action=audit.ADMIN_SMTP_UPDATED,
        request=request,
        actor=admin,
        metadata={"host": config.host, "enabled": enabled},
    )
    await db.commit()
    request.session["flash_notice"] = "SMTP settings saved."
    return RedirectResponse("/admin/smtp", status_code=302)


@router.post("/test")
async def test_smtp(
    request: Request,
    to: str = Form(...),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    config = await get_smtp_config(db)
    if config is None or not config.host or not config.from_address:
        request.session["flash_error"] = "Fill in SMTP host and From address first."
        return RedirectResponse("/admin/smtp", status_code=302)
    try:
        await send_test_email(config, to.strip())
        request.session["flash_notice"] = f"Test email sent to {to}."
    except (SMTPException, OSError) as exc:
        request.session["flash_error"] = f"Send failed: {exc}"
    return RedirectResponse("/admin/smtp", status_code=302)
