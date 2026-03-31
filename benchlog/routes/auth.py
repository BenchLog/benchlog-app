import bcrypt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from benchlog.config import settings
from benchlog.templating import templates

router = APIRouter()

_password_hash: bytes | None = None


def _get_password_hash() -> bytes:
    global _password_hash
    if _password_hash is None:
        _password_hash = bcrypt.hashpw(settings.password.encode(), bcrypt.gensalt())
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

    if username == settings.username and bcrypt.checkpw(password.encode(), _get_password_hash()):
        request.session["user"] = username
        return RedirectResponse("/", status_code=302)

    return templates.TemplateResponse(
        request, "auth/login.html", {"error": "Invalid credentials"}, status_code=401
    )


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
