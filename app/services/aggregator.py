"""Hourly aggregation of raw checks and data retention.

* Raw ``Check`` rows are kept ``raw_retention_hours`` (default 48h); 24h
  metrics are computed from them directly.
* ``HourlyStat`` rows (kept ``hourly_retention_days``) feed 7d / 30d metrics,
  together with raw rows newer than ``agg_until`` (not yet aggregated).
* Aggregating an hour is an upsert on (server, hour), so re-running is
  harmless.  Each run re-processes the last aggregated hour (late inserts)
  and catches up every hour missed while the panel was down.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from typing import Callable

from app.db import db, run_db
from app.models import STATUS_FAIL, STATUS_OK, STATUS_UNKNOWN, Check, HourlyStat, Incident, TelegramOutbox
from app.services.settings import settings
from app.timeutil import DAY, HOUR, hour_floor, now_ts

log = logging.getLogger(__name__)

OUTBOX_RETENTION = 7 * DAY


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[index]


def aggregate_hour(hour: int) -> int:
    """Compute and upsert HourlyStat rows for one hour.  Returns rows written."""
    rows = (Check.select(Check.server, Check.status, Check.latency_ms)
            .where(Check.checked_at >= hour, Check.checked_at < hour + HOUR).tuples())
    buckets: dict[int, dict[str, list[int] | int]] = defaultdict(
        lambda: {"total": 0, "ok": 0, "fail": 0, "unknown": 0, "lat": []})
    for server_id, status, latency in rows:
        b = buckets[server_id]
        b["total"] += 1  # type: ignore[operator]
        if status == STATUS_OK:
            b["ok"] += 1  # type: ignore[operator]
            if latency is not None:
                b["lat"].append(latency)  # type: ignore[union-attr]
        elif status == STATUS_FAIL:
            b["fail"] += 1  # type: ignore[operator]
        elif status == STATUS_UNKNOWN:
            b["unknown"] += 1  # type: ignore[operator]
    payload = []
    for server_id, b in buckets.items():
        lat: list[int] = b["lat"]  # type: ignore[assignment]
        payload.append({
            "server": server_id, "hour": hour, "total": b["total"], "success": b["ok"], "fail": b["fail"],
            "unknown": b["unknown"], "latency_count": len(lat), "latency_sum": float(sum(lat)),
            "latency_sq_sum": float(sum(x * x for x in lat)),
            "avg_latency": (sum(lat) / len(lat)) if lat else None,
            "min_latency": min(lat) if lat else None, "max_latency": max(lat) if lat else None,
            "p95_latency": percentile(lat, 95),
        })
    if payload:
        with db.atomic():
            HourlyStat.delete().where(HourlyStat.hour == hour).execute()
            HourlyStat.insert_many(payload).execute()
    return len(payload)


def run_aggregation(now: int | None = None) -> list[int]:
    """Aggregate every complete hour not yet covered.  Returns hours processed."""
    now = now or now_ts()
    current_hour = hour_floor(now)
    agg_until = int(settings.get("agg_until") or 0)
    oldest = Check.select(Check.checked_at).order_by(Check.checked_at).limit(1).scalar()
    if oldest is None:
        settings.set("agg_until", current_hour, validated=True)
        return []
    start = hour_floor(oldest) if agg_until <= 0 else max(agg_until - HOUR, hour_floor(oldest))
    hours = list(range(start, current_hour, HOUR))
    for hour in hours:
        aggregate_hour(hour)
    settings.set("agg_until", current_hour, validated=True)
    return hours


def cleanup(now: int | None = None) -> dict[str, int]:
    now = now or now_ts()
    raw_cutoff = now - int(settings.get("raw_retention_hours")) * HOUR
    hourly_cutoff = now - int(settings.get("hourly_retention_days")) * DAY
    removed = {
        "checks": Check.delete().where(Check.checked_at < raw_cutoff).execute(),
        "hourly": HourlyStat.delete().where(HourlyStat.hour < hourly_cutoff).execute(),
        "incidents": Incident.delete().where(Incident.ended_at.is_null(False),
                                             Incident.ended_at < hourly_cutoff).execute(),
        "outbox": TelegramOutbox.delete().where(TelegramOutbox.status != "pending",
                                                TelegramOutbox.created_at < now - OUTBOX_RETENTION).execute(),
    }
    return removed


def aggregate_and_cleanup() -> tuple[list[int], dict[str, int]]:
    hours = run_aggregation()
    removed = cleanup()  # always after aggregation, so nothing is lost
    return hours, removed


class Aggregator:
    """Runs at start-up (catch-up) and then a couple of minutes past every hour."""

    OFFSET = 120  # seconds past the hour: lets a slow check cycle finish writing

    def __init__(self, on_done: Callable[[], None] | None = None) -> None:
        self.on_done = on_done
        self.last_run: int | None = None

    async def run_once(self) -> None:
        hours, removed = await run_db(aggregate_and_cleanup)
        self.last_run = now_ts()
        log.info("aggregation", extra={"hours": len(hours), **{f"removed_{k}": v for k, v in removed.items()}})
        if self.on_done:
            self.on_done()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("aggregation failed")
            now = now_ts()
            next_run = hour_floor(now) + HOUR + self.OFFSET
            await asyncio.sleep(max(30, next_run - now))
