import secrets
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC_PREFIXES = (
    "/login",
    "/signup",
    "/logout",
    "/auth/",
    "/static/",
    "/favicon.ico",
)

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_SESSION_KEY = "csrf_token"


def _is_csrf_exempt(path: str) -> bool:
    if path.startswith("/static/"):
        return True
    # OIDC callbacks are externally initiated redirects; state token validation
    # is handled inside the OIDC route itself.
    if path.startswith("/auth/oidc/") and path.endswith("/callback"):
        return True
    return False


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return True
    content_type = request.headers.get("content-type", "")
    return content_type.startswith("application/json")


async def _submitted_token(request: Request) -> str | None:
    header_token = request.headers.get("x-csrf-token")
    if header_token:
        return header_token
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        # Buffer the body, then replay it to the downstream handler so the
        # route can still call request.form() normally.
        body = await request.body()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive
        parsed = parse_qs(body.decode("utf-8", errors="replace"))
        values = parsed.get("_csrf")
        if values:
            return values[0]
    # multipart/form-data is intentionally unsupported — no current route uses
    # it. When uploads are added, reimplement CSRF extraction here so body
    # replay is correct in all branches (the pre-I3 implementation had subtle
    # ordering bugs after request.form() drained the stream).
    return None


class CSRFMiddleware(BaseHTTPMiddleware):
    """Synchronizer token pattern against signed-cookie session.

    - GET/HEAD/OPTIONS: ensure `request.session['csrf_token']` exists.
    - POST/PUT/PATCH/DELETE: require matching `_csrf` form field or
      `X-CSRF-Token` header. Exempts `/static/*` and OIDC callback paths.
    - Rejects `multipart/form-data` with 415 until an upload route needs it.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        session = request.session

        if CSRF_SESSION_KEY not in session:
            session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

        if request.method in UNSAFE_METHODS and not _is_csrf_exempt(path):
            content_type = request.headers.get("content-type", "")
            if content_type.startswith("multipart/form-data"):
                return PlainTextResponse(
                    "multipart/form-data not supported", status_code=415
                )
            expected = session.get(CSRF_SESSION_KEY) or ""
            submitted = await _submitted_token(request) or ""
            if not expected or not submitted or not secrets.compare_digest(
                expected, submitted
            ):
                if _wants_json(request):
                    return JSONResponse(
                        {"detail": "CSRF validation failed"}, status_code=403
                    )
                return PlainTextResponse("CSRF validation failed", status_code=403)

        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate all non-public paths behind a session. Does NOT enforce admin."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if not request.session.get("user"):
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = CSP_POLICY
        scheme = request.url.scheme
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if scheme == "https" or forwarded_proto == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def is_same_origin(request: Request, url: str | None) -> bool:
    """True if `url` is on the same scheme+host+port as the request."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)
