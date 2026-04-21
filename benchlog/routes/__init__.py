from fastapi import FastAPI

from benchlog.routes import (
    account,
    admin,
    auth,
    collections,
    explore,
    export,
    files,
    home,
    links,
    oidc,
    passkeys,
    profile,
    projects,
    updates,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(oidc.router)
    app.include_router(passkeys.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    # Profile (`/u/{username}`) must be registered before `projects`
    # (`/u/{username}/{slug}` lives there) only for readability — paths at
    # different depths can't actually collide. Keep it in this slot so
    # future 2-segment `/u/...` routes land together.
    app.include_router(profile.router)
    # Collections lives under /u/{username}/collections/... — register
    # before `projects` so `/u/{u}/collections/...` doesn't get shadowed
    # by the `/u/{username}/{slug}` project-detail catch-all.
    app.include_router(collections.router)
    app.include_router(projects.router)
    app.include_router(updates.router)
    app.include_router(links.router)
    app.include_router(files.router)
    app.include_router(export.router)
    app.include_router(explore.router)
    app.include_router(home.router)
