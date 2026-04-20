from fastapi import FastAPI

from benchlog.routes import (
    account,
    admin,
    auth,
    explore,
    export,
    files,
    home,
    links,
    oidc,
    passkeys,
    projects,
    updates,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(oidc.router)
    app.include_router(passkeys.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    app.include_router(projects.router)
    app.include_router(updates.router)
    app.include_router(links.router)
    app.include_router(files.router)
    app.include_router(export.router)
    app.include_router(explore.router)
    app.include_router(home.router)
