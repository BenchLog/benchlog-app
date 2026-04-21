import re
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
    "/explore",
)


def _is_public_project_view(method: str, path: str) -> bool:
    """Allow guest GETs for canonical profile + project + journal + link + file URLs.

    - `/u/{username}` — user profile page
    - `/u/{username}/{slug}` — overview tab
    - `/u/{username}/{slug}/journal` — journal tab (full feed)
    - `/u/{username}/{slug}/journal/{entry_slug}` — single entry permalink
    - `/u/{username}/{slug}/links` — links tab
    - `/u/{username}/{slug}/files` — files tab (browser)
    - `/u/{username}/{slug}/files/{id}` — file detail page
    - `/u/{username}/{slug}/files/{id}/download|thumb` — file content endpoints

    Route-level visibility checks enforce private-project 404s. Anything
    with a mutation suffix (`/edit`, `/delete`, `/new`, `/version`) stays
    auth-gated.
    """
    if method != "GET":
        return False
    if not path.startswith("/u/"):
        return False
    parts = path[len("/u/"):].rstrip("/").split("/")
    if not all(parts):
        return False
    # /u/{username}
    if len(parts) == 1:
        return True
    # /u/{username}/collections — list page (before the generic
    # 2-segment case so it's explicit in the whitelist).
    if len(parts) == 2 and parts[1] == "collections":
        return True
    # /u/{username}/{slug}
    if len(parts) == 2:
        return True
    # /u/{username}/collections/{slug} — detail page. Literal "new" is
    # owner-only (the create form), so it stays auth-gated.
    if len(parts) == 3 and parts[1] == "collections" and parts[2] != "new":
        return True
    # /u/{username}/{slug}/{journal|links|files|gallery|export|activity}
    if len(parts) == 3 and parts[2] in {
        "journal", "links", "files", "gallery", "export", "activity"
    }:
        return True
    # /u/{username}/{slug}/journal/{entry_slug} — but not /journal/new
    if len(parts) == 4 and parts[2] == "journal" and parts[3] != "new":
        return True
    # /u/{username}/{slug}/files/{id} — but not /files/new
    if len(parts) == 4 and parts[2] == "files" and parts[3] != "new":
        return True
    # /u/{username}/{slug}/files/{id}/{download|thumb}
    if (
        len(parts) == 5
        and parts[2] == "files"
        and parts[4] in {"download", "thumb"}
    ):
        return True
    return False

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


_MULTIPART_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


def _extract_csrf_from_multipart(body: bytes, content_type: str) -> str | None:
    """Pull the `_csrf` field's value out of a multipart/form-data body.

    We only need a small ASCII text field — CSRF tokens are URL-safe base64
    from `secrets.token_urlsafe`, never spanning multiple lines and never
    containing the boundary. Manual scan keeps us off python-multipart's
    streaming callback API for what would otherwise be ~5 lines of code.
    """
    m = _MULTIPART_BOUNDARY_RE.search(content_type)
    if not m:
        return None
    boundary = ("--" + m.group(1)).encode("ascii")
    for part in body.split(boundary):
        if b'name="_csrf"' not in part:
            continue
        if b"\r\n\r\n" not in part:
            continue
        _headers, _, value_blob = part.partition(b"\r\n\r\n")
        # Each part body ends with the CRLF that precedes the next boundary
        # marker — trim exactly that, not arbitrary whitespace.
        if value_blob.endswith(b"\r\n"):
            value_blob = value_blob[:-2]
        try:
            return value_blob.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


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
        return None
    if content_type.startswith("multipart/form-data"):
        # Same buffer-and-replay pattern as urlencoded — the downstream
        # handler still sees a fresh body via request.form().
        body = await request.body()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive
        return _extract_csrf_from_multipart(body, content_type)
    return None


class CSRFMiddleware(BaseHTTPMiddleware):
    """Synchronizer token pattern against signed-cookie session.

    - GET/HEAD/OPTIONS: ensure `request.session['csrf_token']` exists.
    - POST/PUT/PATCH/DELETE: require matching `_csrf` form field or
      `X-CSRF-Token` header (form bodies may be urlencoded or multipart).
      Exempts `/static/*` and OIDC callback paths.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        session = request.session

        if CSRF_SESSION_KEY not in session:
            session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

        if request.method in UNSAFE_METHODS and not _is_csrf_exempt(path):
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
        if _is_public_project_view(request.method, path):
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
