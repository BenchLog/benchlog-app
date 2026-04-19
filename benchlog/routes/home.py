from fastapi import APIRouter, Depends, Request

from benchlog.dependencies import require_user
from benchlog.models import User
from benchlog.templating import templates

router = APIRouter()


@router.get("/")
async def home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "home.html", {"user": user})
