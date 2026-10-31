"""Admin authentication helpers: bcrypt hashing, CSRF tokens, login rate limiting."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from functools import lru_cache

import bcrypt
from fastapi import Request

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_BYTES = 72  # bcrypt limit


def validate_password(password: str, confirm: str | None = None) -> str | None:
    """Return a Persian error message, or None if acceptable."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        return "Password is too long (72 bytes maximum)."
    if confirm is not None and password != confirm:
        return "Passwords do not match."
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A valid hash to check against when the username does not exist,
    so response time does not reveal which usernames are valid."""
    return hash_password(secrets.token_hex(8))


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


class LoginLimiter:
    """Sliding-window limit of failed logins per client IP and per username."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 900) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        items = self._failures.setdefault(key, deque())
        while items and items[0] < now - self.window:
            items.popleft()
        return items

    def retry_after(self, keys: list[str]) -> int:
        """Seconds until a new attempt is allowed (0 = allowed now)."""
        now = time.monotonic()
        with self._lock:
            waits = [int(items[0] + self.window - now) + 1 for key in keys
                     if len(items := self._recent(key, now)) >= self.max_failures]
        return max(waits, default=0)

    def failure(self, keys: list[str]) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._recent(key, now).append(now)

    def reset(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


login_limiter = LoginLimiter()


def client_ip(request: Request) -> str:
    # uvicorn applies X-Forwarded-For from the trusted local nginx (proxy_headers).
    return request.client.host if request.client else "unknown"
