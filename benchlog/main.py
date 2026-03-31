from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from benchlog.config import settings

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    application = FastAPI(title="BenchLog", description="Project Journal for Makers", version="0.1.0")

    from benchlog.auth import AuthMiddleware

    # AuthMiddleware must be added before SessionMiddleware
    # (Starlette processes middleware in reverse order)
    application.add_middleware(AuthMiddleware)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
    )

    application.mount(
        "/static",
        StaticFiles(directory=BASE_DIR / "static"),
        name="static",
    )

    # Ensure local storage directory exists
    if settings.storage_backend == "local":
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        (settings.storage_path / "files").mkdir(exist_ok=True)
        (settings.storage_path / "images").mkdir(exist_ok=True)
        (settings.storage_path / "thumbnails").mkdir(exist_ok=True)

    from benchlog.routes import register_routes

    register_routes(application)

    return application


app = create_app()
