"""Telegram Bot API calls with route fallback (direct → through online servers).

``auto`` mode: try ``api.telegram.org`` directly with a short (5s) connect
timeout, then through up to 4 online servers' local SOCKS inbounds
(``socks5h`` so DNS is resolved through the tunnel).  The last successful
route is tried first next time.  A server that is the subject of a "down"
alert is excluded for that message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

from app.services import net
from app.services.proxy_pool import ProxyPool
from app.services.settings import settings

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_PROXY_ROUTES = 4  # the first server + 3 more


@dataclass(frozen=True)
class Route:
    kind: str  # direct / proxy
    proxy_url: str | None = None
    server_id: int | None = None
    name: str = ""

    @property
    def key(self) -> str:
        return "direct" if self.kind == "direct" else f"server:{self.server_id}"

    @property
    def label(self) -> str:
        return "مستقیم" if self.kind == "direct" else f"از طریق سرور «{self.name}»"


DIRECT = Route("direct")


@dataclass
class ApiResult:
    ok: bool
    result: Any = None
    route: Route | None = None
    error: str | None = None
    retry_after: int | None = None
    permanent: bool = False  # Telegram rejected the request itself (bad token/chat)
    tried: list[str] = field(default_factory=list)


ClientFactory = Callable[[str | None, httpx.Timeout], httpx.AsyncClient]


def default_client_factory(proxy: str | None, timeout: httpx.Timeout) -> httpx.AsyncClient:
    return net.async_client(proxy, timeout)


class TelegramSender:
    def __init__(self, proxy_pool: ProxyPool | None, client_factory: ClientFactory = default_client_factory,
                 api_base: str = API_BASE) -> None:
        self.proxy_pool = proxy_pool
        self.client_factory = client_factory
        self.api_base = api_base
        self.last_route_key: str | None = None

    async def routes(self, exclude: Iterable[int] = ()) -> list[Route]:
        mode = settings.get("telegram_send_mode")
        routes: list[Route] = []
        if mode in ("auto", "direct"):
            routes.append(DIRECT)
        if mode in ("auto", "via_server") and self.proxy_pool is not None:
            preferred = int(settings.get("telegram_preferred_server") or 0) or None
            candidates = await self.proxy_pool.acandidates(exclude, preferred)
            routes += [Route("proxy", c.url, c.server_id, c.name) for c in candidates[:MAX_PROXY_ROUTES]]
        if self.last_route_key:
            routes.sort(key=lambda r: r.key != self.last_route_key)  # cached route first
        return routes

    async def call(self, method: str, payload: dict[str, Any], exclude: Iterable[int] = (),
                   read_timeout: float = 15.0,
                   payload_for_route: Callable[[Route], dict[str, Any]] | None = None) -> ApiResult:
        """POST to the Bot API, trying each route in order.

        ``payload_for_route`` lets the body depend on the route (the test
        message states which route delivered it).
        """
        token = settings.get("telegram_token")
        if not token:
            return ApiResult(False, error="Bot Token تنظیم نشده است.", permanent=True)
        routes = await self.routes(exclude)
        if not routes:
            return ApiResult(False, error="هیچ مسیر ارسالی وجود ندارد (سرور آنلاینی پیدا نشد).")
        auto = settings.get("telegram_send_mode") == "auto"
        url = f"{self.api_base}/bot{token}/{method}"
        tried: list[str] = []
        for route in routes:
            connect = 5.0 if route.kind == "direct" and auto else 10.0
            timeout = httpx.Timeout(read_timeout, connect=connect)
            try:
                async with self.client_factory(route.proxy_url, timeout) as client:
                    body = payload_for_route(route) if payload_for_route else payload
                    response = await client.post(url, json=body)
            except Exception as exc:  # noqa: BLE001 - try the next route
                tried.append(f"{route.label}: {type(exc).__name__}")
                continue
            try:
                data = response.json()
            except ValueError:
                tried.append(f"{route.label}: HTTP {response.status_code}")
                continue
            if data.get("ok"):
                self.last_route_key = route.key
                return ApiResult(True, data.get("result"), route, tried=tried)
            description = str(data.get("description") or f"HTTP {response.status_code}")
            if response.status_code == 429:
                retry_after = int((data.get("parameters") or {}).get("retry_after") or 5)
                return ApiResult(False, route=route, error=description, retry_after=retry_after, tried=tried)
            # Telegram itself answered; other routes would get the same answer.
            permanent = response.status_code in (400, 401, 403, 404)
            return ApiResult(False, route=route, error=description, permanent=permanent, tried=tried)
        return ApiResult(False, error="ارسال از همه مسیرها ناموفق بود: " + "؛ ".join(tried), tried=tried)
