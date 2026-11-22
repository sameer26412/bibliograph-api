from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import ipaddress
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import bcrypt
from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
import jwt

from app.config import settings

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home")


def hash_password(plain_password: str) -> str:
    raw = plain_password.encode("utf-8")
    if len(raw) > 72:
        raise ValueError("Password cannot exceed 72 UTF-8 bytes.")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    raw = plain_password.encode("utf-8")
    if len(raw) > 72:
        return False
    return bcrypt.checkpw(raw, hashed_password.encode("utf-8"))


def create_access_token(
    *,
    user_id: int,
    role: str,
    expires_minutes: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    ttl = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )


def _is_forbidden_ip(ip_str: str) -> bool:
    clean_ip = ip_str.split("%", 1)[0]
    ip = ipaddress.ip_address(clean_ip)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def validate_public_http_url(url: str, resolve_dns: bool = False) -> str:
    """Validate URL scheme/host and block internal, loopback, private, and link-local IPs."""
    cleaned = url.strip()
    if len(cleaned) > 500:
        raise ValueError("URL exceeds maximum length of 500 characters.")

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are permitted.")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname.")
    if parsed.username or parsed.password:
        raise ValueError("Embedded credentials in URLs are not permitted.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_SUFFIXES):
        raise ValueError("Internal or local hostnames are blocked.")

    # Direct IP literal check (always runs, even without DNS resolution)
    try:
        if _is_forbidden_ip(hostname):
            raise ValueError("Private, loopback, or non-public IP addresses are blocked.")
        return cleaned
    except ValueError as exc:
        if "blocked" in str(exc):
            raise

    if resolve_dns:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("Could not resolve external hostname.") from exc

        if not addr_info:
            raise ValueError("Hostname resolved to no IP addresses.")

        for _, _, _, _, sockaddr in addr_info:
            resolved_ip = sockaddr[0]
            if _is_forbidden_ip(resolved_ip):
                raise ValueError("Resolved IP address is private or non-public.")

    return cleaned


def verify_external_url(url: str) -> dict[str, Any]:
    """Safely check a public external reference URL with strict SSRF and timeout guards."""
    safe_url = validate_public_http_url(url, resolve_dns=True)
    timeout = httpx.Timeout(
        connect=min(2.0, settings.outbound_timeout_seconds),
        read=settings.outbound_timeout_seconds,
        write=settings.outbound_timeout_seconds,
        pool=min(2.0, settings.outbound_timeout_seconds),
    )

    started = time.perf_counter()
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "BiblioGraph-API-LinkVerifier/1.0"},
    ) as client:
        response = client.head(safe_url)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    return {
        "url": safe_url,
        "reachable": 200 <= response.status_code < 400,
        "status_code": response.status_code,
        "latency_ms": elapsed_ms,
        "content_type": response.headers.get("content-type"),
    }


def extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter (pairs with Vercel WAF at the edge)."""

    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int) -> tuple[bool, int, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
                return False, 0, retry_after

            bucket.append(now)
            remaining = max(0, limit - len(bucket))

            # Bound dictionary growth on long-lived processes
            if len(self._hits) > 2048:
                stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
                for k in stale:
                    del self._hits[k]

            return True, remaining, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(window_seconds=60)


class MaxBodySizeMiddleware:
    """Pure ASGI guard that blocks oversized payloads before JSON parsing."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": f"Request payload exceeds {self.max_bytes} bytes."},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise PayloadTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except PayloadTooLargeError:
            response = JSONResponse(
                status_code=413,
                content={"detail": f"Request payload exceeds {self.max_bytes} bytes."},
            )
            await response(scope, receive, send)


class PayloadTooLargeError(Exception):
    pass