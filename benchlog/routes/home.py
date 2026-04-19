from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/")
async def home():
    # AuthMiddleware already gates this path; unauthenticated requests are
    # redirected to /login before reaching here.
    return RedirectResponse("/projects", status_code=302)
