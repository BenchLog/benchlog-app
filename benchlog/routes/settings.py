import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.models.user import User
from benchlog.templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()

    return templates.TemplateResponse(request, "settings/index.html", {
        "user": user,
        "message": None,
    })


@router.post("/settings", response_class=HTMLResponse)
async def update_settings(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse("User not found", status_code=404)

    form = await request.form()

    display_name = form.get("display_name", "").strip()
    if display_name:
        user.display_name = display_name

    email = form.get("email", "").strip()
    user.email = email or None

    bio = form.get("bio", "").strip()
    user.bio = bio or None

    # Password change
    current_pw = form.get("current_password", "")
    new_pw = form.get("new_password", "")
    confirm_pw = form.get("confirm_password", "")

    message = "Settings saved."
    if new_pw:
        if not current_pw or not _bcrypt.checkpw(current_pw.encode(), user.password_hash.encode()):
            message = "Current password is incorrect."
        elif new_pw != confirm_pw:
            message = "New passwords do not match."
        elif len(new_pw) < 4:
            message = "Password must be at least 4 characters."
        else:
            user.password_hash = _bcrypt.hashpw(new_pw.encode(), _bcrypt.gensalt()).decode()
            message = "Settings and password saved."

    await db.commit()

    return templates.TemplateResponse(request, "settings/index.html", {
        "user": user,
        "message": message,
    })
