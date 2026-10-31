"""Subscription links: fetch (direct, then through online servers) and sync.

Servers are matched by identity (protocol + address + port + credential), so
a renamed or re-pathed config keeps its history.  Configs that disappear
from the subscription are deactivated, not deleted.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from app.db import db, run_db
from app.models import Server, Subscription
from app.services import net
from app.services.parser import LinkParseError, decode_subscription, parse_many
from app.services.proxy_pool import ProxyPool
from app.services.servers import apply_parsed, create_from_parsed
from app.timeutil import now_ts

log = logging.getLogger(__name__)

USER_AGENT = "v2rayN/7.0"  # most panels return a Base64 list for this UA
MAX_SIZE = 5 * 1024 * 1024


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    deactivated: list[int] = field(default_factory=list)
    reactivated: int = 0
    errors: list[tuple[int, str, str]] = field(default_factory=list)
    via: str = ""


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Subscription URL must start with http:// or https://.")
    return url


async def fetch_text(url: str, proxy_pool: ProxyPool | None, timeout: float = 20.0) -> tuple[str, str]:
    """Return (content, route label).  Tries direct first, then up to 3 servers."""
    routes: list[tuple[str | None, str]] = [(None, "direct")]
    if proxy_pool is not None:
        routes += [(c.url, f"server {c.name}") for c in (await proxy_pool.acandidates())[:3]]
    errors = []
    for proxy, label in routes:
        try:
            async with net.async_client(proxy, httpx.Timeout(timeout, connect=8), follow_redirects=True,
                                        headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(url)
            if response.status_code != 200:
                errors.append(f"{label}: HTTP {response.status_code}")
                continue
            if len(response.content) > MAX_SIZE:
                raise ValueError("Subscription content is too large.")
            return response.text, label
        except (httpx.HTTPError, OSError) as exc:
            errors.append(f"{label}: {type(exc).__name__}")
    raise ValueError("Could not fetch the subscription: " + "; ".join(errors))


def apply_subscription(sub_id: int, content: str) -> SyncResult:
    """Sync DB servers of a subscription with its content (sync)."""
    subscription = Subscription.get_by_id(sub_id)
    bulk = parse_many(decode_subscription(content))
    result = SyncResult(errors=bulk.errors)
    current = {s.identity: s for s in Server.select().where(Server.subscription == sub_id)}
    others = {i for (i,) in Server.select(Server.identity).where(
        (Server.subscription != sub_id) | Server.subscription.is_null()).tuples()}
    seen: set[str] = set()
    with db.atomic():
        for parsed in bulk.links:
            if parsed.identity in seen or parsed.identity in others:
                continue
            seen.add(parsed.identity)
            server = current.get(parsed.identity)
            if server is None:
                create_from_parsed(parsed, is_public=subscription.make_public, subscription_id=sub_id)
                result.added += 1
                continue
            changed = server.raw_link != parsed.raw
            if changed:
                apply_parsed(server, parsed)
                result.updated += 1
            if not server.is_active:
                server.is_active = True
                result.reactivated += 1
                changed = True
            if changed:
                server.updated_at = now_ts()
                server.save()
        for identity, server in current.items():
            if identity not in seen and server.is_active:
                server.is_active = False
                server.save()
                result.deactivated.append(server.id)
        subscription.last_sync = now_ts()
        subscription.last_error = None
        subscription.last_count = len(seen)
        subscription.save()
    return result


def record_error(sub_id: int, message: str) -> None:
    Subscription.update(last_error=message[:500], last_sync=now_ts()).where(Subscription.id == sub_id).execute()


def due_subscriptions(now: int) -> list[int]:
    return [s.id for s in Subscription.select().where(Subscription.auto_sync == True)  # noqa: E712
            if s.last_sync is None or s.last_sync + s.interval_minutes * 60 <= now]


class SubscriptionService:
    def __init__(self, proxy_pool: ProxyPool | None,
                 on_change: Callable[[list[int]], Awaitable[None]]) -> None:
        self.proxy_pool = proxy_pool
        self.on_change = on_change
        self._lock = asyncio.Lock()

    async def sync(self, sub_id: int) -> SyncResult:
        async with self._lock:
            subscription = await run_db(Subscription.get_by_id, sub_id)
            try:
                content, via = await fetch_text(subscription.url, self.proxy_pool)
                result = await run_db(apply_subscription, sub_id, content)
                result.via = via
            except (ValueError, LinkParseError) as exc:
                await run_db(record_error, sub_id, str(exc))
                raise
            if result.added or result.updated or result.deactivated or result.reactivated:
                await self.on_change(result.deactivated)
            log.info("subscription synced", extra={"id": sub_id, "added": result.added, "updated": result.updated,
                                                   "deactivated": len(result.deactivated)})
            return result

    async def run_forever(self) -> None:
        await asyncio.sleep(30)  # let Xray and the first check cycle come up
        while True:
            try:
                for sub_id in await run_db(due_subscriptions, now_ts()):
                    try:
                        await self.sync(sub_id)
                    except (ValueError, LinkParseError) as exc:
                        log.warning("subscription sync failed", extra={"id": sub_id, "error": str(exc)[:300]})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("subscription loop failed")
            await asyncio.sleep(60)
