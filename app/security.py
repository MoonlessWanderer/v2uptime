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
    """Sliding-window limit of failed logins.

    ``max_failures`` is the default ceiling. ``limits`` overrides it for a key
    prefix (the text before the first colon), so one IP and one username can
    have different ceilings in the same window.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 900,
                 limits: dict[str, int] | None = None) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self.limits = dict(limits or {})
        self._failures: dict[str, deque[float]] = {}
        self._alert_at: float | None = None
        self._lock = threading.Lock()

    def _ceiling(self, key: str) -> int:
        kind, _, _ = key.partition(":")
        return self.limits.get(kind, self.max_failures)

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
                     if len(items := self._recent(key, now)) >= self._ceiling(key)]
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

    def claim_security_alert(self) -> bool:
        """True the first time in this window. Further calls wait the window out."""
        now = time.monotonic()
        with self._lock:
            if self._alert_at is not None and now - self._alert_at < self.window:
                return False
            self._alert_at = now
            return True


login_limiter = LoginLimiter(limits={"user": 50})


def client_ip(request: Request) -> str:
    # uvicorn applies X-Forwarded-For from the trusted local nginx (proxy_headers).
    return request.client.host if request.client else "unknown"


TRUSTED_IP_DAYS = 30
TRUSTED_IP_LIMIT = 20


def is_trusted_ip(admin_id: int, ip: str, now: int | None = None) -> bool:
    """True when this IP signed in for this admin during the last 30 days."""
    from app.models import TrustedLogin
    from app.timeutil import now_ts

    now = now_ts() if now is None else now
    row = TrustedLogin.get_or_none((TrustedLogin.admin == admin_id) & (TrustedLogin.ip == ip))
    return bool(row and now - row.last_success <= TRUSTED_IP_DAYS * 86400)


def remember_trusted_ip(admin_id: int, ip: str, now: int | None = None) -> None:
    """Record a successful sign-in. Only the 20 most recent IPs are kept."""
    from app.db import db
    from app.models import TrustedLogin
    from app.timeutil import now_ts

    now = now_ts() if now is None else now
    with db.atomic():
        row = TrustedLogin.get_or_none((TrustedLogin.admin == admin_id) & (TrustedLogin.ip == ip))
        if row is None:
            TrustedLogin.create(admin=admin_id, ip=ip, last_success=now)
        else:
            row.last_success = now
            row.save()
        kept = [item.id for item in TrustedLogin.select(TrustedLogin.id)
                .where(TrustedLogin.admin == admin_id)
                .order_by(TrustedLogin.last_success.desc(), TrustedLogin.id.desc())]
        if len(kept) > TRUSTED_IP_LIMIT:
            TrustedLogin.delete().where(TrustedLogin.id.in_(kept[TRUSTED_IP_LIMIT:])).execute()


def clear_trusted_ips(admin_id: int) -> None:
    """Forget every trusted IP for this admin. Called when the password changes."""
    from app.models import TrustedLogin

    TrustedLogin.delete().where(TrustedLogin.admin == admin_id).execute()
