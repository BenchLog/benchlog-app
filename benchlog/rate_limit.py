"""In-memory sliding-window rate limiter for auth endpoints.

Single-worker self-hosted deployments don't need Redis. Losing limiter
state on restart is fine (arguably desirable) for auth brute-force
defense. Keyed by (bucket, client_ip).
"""

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status

from benchlog.config import settings


class RateLimiter:
    """In-memory sliding-window limiter keyed by (bucket, key)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, bucket: str, key: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits[(bucket, key)]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = int(hits[0] + window_seconds - now) + 1
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Try again later.",
                    headers={"Retry-After": str(max(retry_after, 1))},
                )
            hits.append(now)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when opted in — otherwise clients can spoof it to bypass per-IP limits.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit: int, window_seconds: int):
    """FastAPI dependency factory. Keyed by (bucket, client_ip)."""

    def _dep(request: Request) -> None:
        limiter.check(bucket, client_ip(request), limit, window_seconds)

    return _dep
