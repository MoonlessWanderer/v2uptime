"""Shared outbound HTTP client setup.

Building an SSL context loads the whole CA bundle, which costs tens of
milliseconds of blocking CPU (≈120 ms on a slow machine).  Doing that for
every check stalled the event loop and was counted in the latency of every
concurrent check, so the context is built once per process and reused.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from typing import Any

import certifi
import httpx


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """One shared, verifying SSL context (same CA bundle httpx uses by default)."""
    return ssl.create_default_context(cafile=certifi.where())


def async_client(proxy: str | None = None, timeout: httpx.Timeout | float = 10.0, **kwargs: Any) -> httpx.AsyncClient:
    """AsyncClient that ignores proxy env vars and reuses :func:`ssl_context`."""
    return httpx.AsyncClient(proxy=proxy, timeout=timeout, verify=ssl_context(), trust_env=False, **kwargs)
