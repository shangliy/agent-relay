"""HTTP hardening: per-IP rate limits, body size cap, security headers.

In-memory limits assume a single server process (the pilot runs one Fly
machine). Move to a shared store before scaling out.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

MAX_BODY_BYTES = 1_000_000

# (bucket, limit, window seconds). Device polling (~8s) + heartbeats stay well under "read".
LIMITS = {
    "auth": (20, 60),
    "write": (120, 60),
    "read": (600, 60),
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com"
)


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        ip = request.headers.get("fly-client-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


class RateLimiter:
    def __init__(self):
        self.hits: dict[tuple[str, str], deque] = {}
        self.lock = threading.Lock()
        self.last_gc = time.monotonic()

    def allow(self, bucket: str, key: str) -> bool:
        limit, window = LIMITS[bucket]
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault((bucket, key), deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if now - self.last_gc > 300:
                self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - 120}
                self.last_gc = now
            return True


def bucket_for(request: Request) -> str | None:
    path = request.url.path
    if path.startswith("/v2/auth/"):
        return "auth"
    if not path.startswith("/v2/"):
        return None
    return "read" if request.method in ("GET", "HEAD") else "write"


class HardeningMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings, limiter: RateLimiter | None = None):
        super().__init__(app)
        self.settings = settings
        self.limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body too large"}}, status_code=413)
        bucket = bucket_for(request)
        if bucket and self.settings.rate_limits:
            if not self.limiter.allow(bucket, client_ip(request, self.settings.trust_proxy)):
                return JSONResponse({"error": {"code": "RATE_LIMITED", "message": "Too many requests; slow down"}},
                                    status_code=429, headers={"Retry-After": "30"})
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Content-Security-Policy", CSP)
        if self.settings.secure_cookies:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith("/v2/"):
            h.setdefault("Cache-Control", "no-store")
        return response
