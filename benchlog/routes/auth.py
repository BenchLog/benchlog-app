from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from passlib.hash import bcrypt

from benchlog.config import settings
from benchlog.templating import templates

router = APIRouter()

# Hash the configured password on startup
_password_hash: str | None = None


def _get_password_hash() -> str:
    global _password_hash
    if _password_hash is None:
        _password_hash = bcrypt.hash(settings.password)
    return _password_hash


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    if username == settings.username and bcrypt.verify(password, _get_password_hash()):
        request.session["user"] = username
        return RedirectResponse("/", status_code=302)

    return templates.TemplateResponse(
        request, "auth/login.html", {"error": "Invalid credentials"}, status_code=401
    )


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
