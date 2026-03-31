from fastapi import FastAPI

from benchlog.routes.auth import router as auth_router
from benchlog.routes.bom import router as bom_router
from benchlog.routes.files import router as files_router
from benchlog.routes.images import router as images_router
from benchlog.routes.links import router as links_router
from benchlog.routes.projects import router as projects_router
from benchlog.routes.search import router as search_router
from benchlog.routes.settings import router as settings_router
from benchlog.routes.tags import router as tags_router
from benchlog.routes.updates import router as updates_router


def register_routes(app: FastAPI) -> None:
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(files_router)
    app.include_router(updates_router)
    app.include_router(bom_router)
    app.include_router(links_router)
    app.include_router(tags_router)
    app.include_router(images_router)
    app.include_router(search_router)
    app.include_router(settings_router)
