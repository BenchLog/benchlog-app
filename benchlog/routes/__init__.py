from fastapi import FastAPI

from benchlog.routes.auth import router as auth_router
from benchlog.routes.files import router as files_router
from benchlog.routes.projects import router as projects_router
from benchlog.routes.tags import router as tags_router


def register_routes(app: FastAPI) -> None:
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(files_router)
    app.include_router(tags_router)
