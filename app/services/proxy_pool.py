"""Use monitored servers as outbound proxies for the panel itself.

The panel runs inside Iran, where Telegram, GitHub, and subscription hosts may
be unreachable directly.  Every server's local SOCKS inbound in Xray is a
working tunnel, so other parts (Telegram, one-time subscription fetches, updates
via ``/internal/proxy``) pick one with :meth:`ProxyPool.candidates`.

Order: preferred server (if online) → online servers by highest stability
score, then lowest latency → degraded servers as a last resort.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.db import run_db
from app.services.checker import socks_url
from app.services.stats import StatsService
from app.services.status import DEGRADED, ONLINE
from app.services.xray_manager import XrayManager


@dataclass(frozen=True)
class ProxyCandidate:
    server_id: int
    name: str
    port: int
    status: str
    stability: float | None
    latency: float | None

    @property
    def url(self) -> str:
        return socks_url(self.port)


def select_candidates(items: Iterable[ProxyCandidate], exclude: Iterable[int] = (), preferred: int | None = None,
                      include_degraded: bool = True) -> list[ProxyCandidate]:
    excluded = set(exclude)
    usable = [c for c in items if c.server_id not in excluded]

    def order(c: ProxyCandidate) -> tuple[float, float, int]:
        return (-(c.stability or 0.0), c.latency if c.latency is not None else float("inf"), c.server_id)

    online = sorted((c for c in usable if c.status == ONLINE), key=order)
    if preferred:
        online.sort(key=lambda c: c.server_id != preferred)  # stable: keeps the rest ordered
    result = online
    if include_degraded:
        result += sorted((c for c in usable if c.status == DEGRADED), key=order)
    return result


class ProxyPool:
    def __init__(self, xray: XrayManager, stats: StatsService) -> None:
        self.xray = xray
        self.stats = stats

    def candidates(self, exclude: Iterable[int] = (), preferred: int | None = None) -> list[ProxyCandidate]:
        """Sync (may hit the DB on a stats cache miss): call via ``run_db`` from async code."""
        ports = self.xray.loaded_ports()
        if not ports:
            return []
        metrics = self.stats.server_metrics()
        items = []
        for server_id, port in ports.items():
            m = metrics.get(server_id)
            if m is None:
                continue
            items.append(ProxyCandidate(server_id, m.name, port, self.stats.current_status(server_id),
                                        m.stability, m.median_1h if m.median_1h is not None else m.latency_ms))
        return select_candidates(items, exclude, preferred)

    def get_healthy_proxy(self, exclude: Iterable[int] = ()) -> ProxyCandidate | None:
        found = self.candidates(exclude)
        return found[0] if found else None

    async def acandidates(self, exclude: Iterable[int] = (), preferred: int | None = None) -> list[ProxyCandidate]:
        return await run_db(self.candidates, list(exclude), preferred)
