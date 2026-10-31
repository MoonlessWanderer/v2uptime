"""Current server status (single definition used everywhere).

* **offline**  – the server has an open incident (N consecutive failures;
  closed again after M consecutive successes).
* **degraded** – not offline, but the last check failed, or its latency is
  above ``degraded_latency_ms``, or more than ``degraded_fail_percent`` of
  the checks in the last 10 minutes failed.
* **online**   – last check succeeded with latency under the threshold.
* **unknown**  – not enough data yet, or no valid check recently (e.g. Xray
  or the monitor's own network was down).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from app.models import STATUS_FAIL, STATUS_OK, STATUS_UNKNOWN

ONLINE = "online"
DEGRADED = "degraded"
OFFLINE = "offline"
UNKNOWN = "unknown"

STATUS_LABELS = {ONLINE: "Online", DEGRADED: "Degraded", OFFLINE: "Down", UNKNOWN: "Unknown"}
STATUS_EMOJI = {ONLINE: "🟢", DEGRADED: "🟡", OFFLINE: "🔴", UNKNOWN: "⚪"}
NEW_LABEL = "New"

MIN_VALID_FOR_STATUS = 2
RECENT_WINDOW = 3600
DEGRADED_WINDOW = 600


@dataclass(frozen=True)
class LivePoint:
    ts: int
    status: str  # ok / fail / unknown
    latency_ms: int | None
    error_type: str | None = None


def classify_status(points: Iterable[LivePoint], in_incident: bool, now: int, *, interval: int,
                    degraded_latency_ms: int, degraded_fail_percent: int) -> str:
    if in_incident:
        return OFFLINE
    valid = [p for p in points if p.status != STATUS_UNKNOWN]
    if len(valid) < MIN_VALID_FOR_STATUS:
        return UNKNOWN
    last = valid[-1]
    if now - last.ts > max(3 * interval, 300):
        return UNKNOWN
    if last.status == STATUS_FAIL:
        return DEGRADED
    window = [p for p in valid if p.ts >= now - DEGRADED_WINDOW]
    fail_ratio = sum(p.status == STATUS_FAIL for p in window) / len(window) if window else 0.0
    if (last.latency_ms or 0) > degraded_latency_ms or fail_ratio * 100 > degraded_fail_percent:
        return DEGRADED
    return ONLINE


def display_label(status: str, collecting: bool) -> str:
    """Label for the public page and API: a server still collecting data is "New"."""
    return NEW_LABEL if collecting else STATUS_LABELS[status]


class LiveRegistry:
    """In-memory recent results per server (last hour), seeded from the DB."""

    def __init__(self) -> None:
        self._points: dict[int, deque[LivePoint]] = {}
        self._lock = threading.Lock()

    def seed(self, rows: Iterable[tuple[int, int, str, int | None, str | None]]) -> None:
        """rows: (server_id, checked_at, status, latency_ms, error_type), chronological."""
        with self._lock:
            self._points.clear()
            for server_id, ts, status, latency, error in rows:
                self._points.setdefault(server_id, deque()).append(LivePoint(ts, status, latency, error))

    def add(self, server_id: int, point: LivePoint) -> None:
        with self._lock:
            points = self._points.setdefault(server_id, deque())
            points.append(point)
            cutoff = point.ts - RECENT_WINDOW
            while points and points[0].ts < cutoff:
                points.popleft()

    def remove(self, server_id: int) -> None:
        with self._lock:
            self._points.pop(server_id, None)

    def points(self, server_id: int) -> list[LivePoint]:
        with self._lock:
            return list(self._points.get(server_id, ()))

    def last_valid(self, server_id: int) -> LivePoint | None:
        for point in reversed(self.points(server_id)):
            if point.status != STATUS_UNKNOWN:
                return point
        return None

    def last_check_ts(self) -> int | None:
        with self._lock:
            stamps = [pts[-1].ts for pts in self._points.values() if pts]
        return max(stamps) if stamps else None

    def recent_latencies(self, server_id: int, since: int) -> list[int]:
        return [p.latency_ms for p in self.points(server_id)
                if p.ts >= since and p.status == STATUS_OK and p.latency_ms is not None]


def seed_rows(since: int) -> list[tuple[int, int, str, int | None, str | None]]:
    """Load recent checks for :meth:`LiveRegistry.seed` (sync, DB)."""
    from app.models import Check

    query = (Check.select(Check.server, Check.checked_at, Check.status, Check.latency_ms, Check.error_type)
             .where(Check.checked_at >= since).order_by(Check.checked_at).tuples())
    return list(query)


__all__ = [
    "ONLINE", "DEGRADED", "OFFLINE", "UNKNOWN", "STATUS_LABELS", "STATUS_EMOJI", "NEW_LABEL", "LivePoint",
    "LiveRegistry", "classify_status", "display_label", "seed_rows", "STATUS_OK", "STATUS_FAIL",
]
