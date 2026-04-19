import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from benchlog.config import settings
from benchlog.middleware import (
    AuthMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    is_same_origin,
)
from benchlog.templating import templates

ERROR_COPY: dict[int, tuple[str, str]] = {
    400: ("Bad request", "That request couldn't be understood."),
    401: ("Sign in required", "Your session has expired or you're not signed in."),
    403: ("Not allowed", "You don't have access to this page."),
    404: ("Page not found", "We couldn't find what you were looking for."),
    405: ("Method not allowed", "That action isn't supported here."),
    500: ("Something went wrong", "An unexpected error occurred on our end."),
}

BASE_DIR = Path(__file__).resolve().parent

SESSION_MAX_AGE = 60 * 60 * 24 * 14

logger = logging.getLogger("benchlog")


def _is_local_dev(base_url: str) -> bool:
    return base_url.startswith("http://localhost") or base_url.startswith("http://127.0.0.1")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Deferred imports so test conftest can swap the engine before the session
    # factory is resolved.
    from benchlog.database import async_session
    from benchlog.bootstrap import seed_initial_config

    async with async_session() as db:
        try:
            await seed_initial_config(db, settings)
        except SQLAlchemyError:
            logger.warning("initial config seeding failed", exc_info=True)
    yield


def create_app() -> FastAPI:
    # Localhost dev is exempt so the default secret in config.py still works.
    if not _is_local_dev(settings.base_url):
        if (
            settings.secret_key == "change-me"
            or "change-me" in settings.secret_key
            or len(settings.secret_key) < 32
        ):
            raise RuntimeError(
                "BENCHLOG_SECRET_KEY must be set to a strong random value "
                "(>=32 chars, no 'change-me') when base_url is not localhost."
            )

    app = FastAPI(
        title="BenchLog",
        description="Project Journal for Makers",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # Order matters — session must be on the outside so AuthMiddleware sees it.
    # last-added = outermost, so CSRF is added first (innermost), then Auth:
    # request flow is SecurityHeaders -> Session -> Auth -> CSRF -> handler.
    # This way unauthenticated POSTs redirect to /login before being CSRF-checked.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(AuthMiddleware)
    # Starlette's SessionMiddleware hardcodes HttpOnly on the session cookie;
    # it can't be turned off here.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
        max_age=SESSION_MAX_AGE,
    )
    # Last-added = outermost, so security headers land on every response.
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 429 and wants_html:
            request.session["flash_error"] = (
                "Too many attempts. Please wait a bit and try again."
            )
            referer = request.headers.get("referer")
            target = referer if is_same_origin(request, referer) else "/login"
            return RedirectResponse(target, status_code=303)
        if wants_html and exc.status_code >= 400:
            heading, message = ERROR_COPY.get(
                exc.status_code, ("Something went wrong", "An unexpected error occurred.")
            )
            signed_in = bool(request.session.get("user"))
            home_href = "/" if signed_in else "/login"
            home_label = "Return home" if signed_in else "Go to sign in"
            return templates.TemplateResponse(
                request,
                "errors/error.html",
                {
                    "status_code": exc.status_code,
                    "heading": heading,
                    "message": message,
                    "home_href": home_href,
                    "home_label": home_label,
                },
                status_code=exc.status_code,
            )
        return await http_exception_handler(request, exc)

    from benchlog.routes import register_routes

    register_routes(app)
    return app


app = create_app()
