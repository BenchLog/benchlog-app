from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from benchlog.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def project_list(request: Request):
    return templates.TemplateResponse(request, "projects/list.html", {"projects": []})
