from fastapi import FastAPI

from benchlog.routes import account, admin, auth, home, oidc, passkeys


def register_routes(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(oidc.router)
    app.include_router(passkeys.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    app.include_router(home.router)
