"""Metrics, ranking, status bars and history.

Definitions (documented in README):

* **Uptime %** = ok / (ok + fail).  ``unknown`` checks are ignored.
  24h comes from raw checks; 7d / 30d from ``HourlyStat`` plus raw checks
  newer than the aggregator's progress (``agg_until``).
* **Ping** (public list) = median latency of successful checks in the last
  10 minutes, or the last value when there are none.
* **Speed** = median / mean latency of successful checks in the last hour.
* **Jitter** = standard deviation of successful-check latency over 24h.
* **Stability score** (0-100)::

      normalized_jitter = min(jitter_24h / 500ms, 1)
      stability_score   = (uptime_24h * 0.7 + (1 - normalized_jitter) * 0.3) * 100

  where ``uptime_24h`` is a fraction (0..1).
* Servers with fewer than ``MIN_CHECKS_FOR_RANKING`` valid checks in 24h are
  "collecting data" and excluded from rankings.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.db import db
from app.models import Incident, Server
from app.services.settings import settings
from app.services.status import (
    DEGRADED,
    OFFLINE,
    ONLINE,
    UNKNOWN,
    LiveRegistry,
    classify_status,
    display_label,
)
from app.services.aggregator import percentile
from app.services.checker import error_label
from app.timeutil import (
    DAY,
    HOUR,
    add_local_days,
    fmt_date,
    fmt_datetime,
    fmt_duration,
    fmt_range,
    hour_floor,
    iso_utc,
    local_midnight,
    now_ts,
    tz_offset,
    utc_offset,
)

RANGES: dict[str, int] = {"24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
RANGE_LABELS = {"24h": "24h", "7d": "7d", "30d": "30d"}
BAR_SPEC: dict[str, tuple[int, int]] = {"24h": (48, 1800), "7d": (42, 4 * HOUR), "30d": (30, DAY)}
HISTORY_STEP: dict[str, int] = {"24h": 300, "7d": HOUR, "30d": 6 * HOUR}
MIN_CHECKS_FOR_RANKING = 10
PING_WINDOW = 600  # public ping: median of the successful checks in the last 10 minutes
JITTER_REFERENCE_MS = 500.0
CACHE_TTL = 60.0


# --------------------------------------------------------------- pure math

@dataclass
class Counts:
    ok: int = 0
    fail: int = 0
    unknown: int = 0
    lat_count: int = 0
    lat_sum: float = 0.0
    lat_sq: float = 0.0
    lat_min: int | None = None
    lat_max: int | None = None

    def add(self, other: "Counts") -> None:
        self.ok += other.ok
        self.fail += other.fail
        self.unknown += other.unknown
        self.lat_count += other.lat_count
        self.lat_sum += other.lat_sum
        self.lat_sq += other.lat_sq
        for attr, pick in (("lat_min", min), ("lat_max", max)):
            mine, theirs = getattr(self, attr), getattr(other, attr)
            if theirs is not None:
                setattr(self, attr, theirs if mine is None else pick(mine, theirs))

    @property
    def valid(self) -> int:
        return self.ok + self.fail

    @property
    def uptime(self) -> float | None:
        return uptime_percent(self.ok, self.fail)

    @property
    def avg_latency(self) -> float | None:
        return self.lat_sum / self.lat_count if self.lat_count else None

    @property
    def jitter(self) -> float | None:
        if self.lat_count < 2:
            return None
        mean = self.lat_sum / self.lat_count
        return math.sqrt(max(0.0, self.lat_sq / self.lat_count - mean * mean))


def uptime_percent(ok: int, fail: int) -> float | None:
    total = ok + fail
    return ok / total * 100 if total else None


def stability_score(uptime_pct: float | None, jitter_ms: float | None) -> float | None:
    if uptime_pct is None:
        return None
    normalized_jitter = min((jitter_ms or 0.0) / JITTER_REFERENCE_MS, 1.0)
    return round((uptime_pct / 100 * 0.7 + (1 - normalized_jitter) * 0.3) * 100, 1)


def median(values: Iterable[float]) -> float | None:
    values = list(values)
    return float(statistics.median(values)) if values else None


def bar_level(uptime: float | None) -> str:
    if uptime is None:
        return "none"
    if uptime >= 99:
        return "up"
    if uptime >= 95:
        return "minor"
    if uptime >= 80:
        return "major"
    return "down"


@dataclass
class ServerMetrics:
    server_id: int
    name: str = ""
    status: str = UNKNOWN
    latency_ms: int | None = None
    ping_ms: int | None = None
    last_check_at: int | None = None
    last_error: str | None = None
    uptime_24h: float | None = None
    uptime_7d: float | None = None
    uptime_30d: float | None = None
    median_1h: float | None = None
    avg_1h: float | None = None
    jitter_24h: float | None = None
    stability: float | None = None
    valid_24h: int = 0
    incidents_24h: int = 0

    @property
    def collecting(self) -> bool:
        return self.valid_24h < MIN_CHECKS_FOR_RANKING


def rank_fastest(metrics: Iterable[ServerMetrics]) -> list[int]:
    """Online servers with enough data, by 1h median latency."""
    eligible = [m for m in metrics if m.status == ONLINE and not m.collecting and m.median_1h is not None]
    eligible.sort(key=lambda m: (m.median_1h, -(m.stability or 0), m.server_id))
    return [m.server_id for m in eligible]


def rank_stable(metrics: Iterable[ServerMetrics]) -> list[int]:
    """Servers with enough data, by 24h stability score."""
    eligible = [m for m in metrics if not m.collecting and m.stability is not None]
    eligible.sort(key=lambda m: (-(m.stability or 0), m.median_1h if m.median_1h is not None else math.inf,
                                 m.server_id))
    return [m.server_id for m in eligible]


@dataclass(frozen=True)
class Bucket:
    start: int
    end: int


def fixed_buckets(now: int, size: int, count: int, tz: str) -> list[Bucket]:
    """``count`` buckets of ``size`` seconds aligned to local time, last one holding ``now``."""
    offset = utc_offset(now, tz)
    end = ((now + offset) // size + 1) * size - offset
    return [Bucket(end - (count - i) * size, end - (count - i - 1) * size) for i in range(count)]


def day_buckets(now: int, count: int, tz: str) -> list[Bucket]:
    today = local_midnight(now, tz)
    starts = [add_local_days(today, -i, tz) for i in range(count - 1, -1, -1)]
    return [Bucket(s, add_local_days(s, 1, tz)) for s in starts]


def make_buckets(range_key: str, now: int, tz: str) -> list[Bucket]:
    count, size = BAR_SPEC[range_key]
    return day_buckets(now, count, tz) if size == DAY else fixed_buckets(now, size, count, tz)


@dataclass
class IncidentStats:
    count: int = 0
    downtime: int = 0
    mttr: float | None = None
    longest: int = 0
    _closed: list[int] = field(default_factory=list, repr=False)


# ------------------------------------------------------------ DB queries

_COUNT_COLUMNS = (
    "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END), COUNT(latency_ms), COALESCE(SUM(latency_ms), 0), "
    "COALESCE(SUM(latency_ms * latency_ms), 0), MIN(latency_ms), MAX(latency_ms)"
)
_HOURLY_COLUMNS = (
    "SUM(success), SUM(fail), SUM(unknown), SUM(latency_count), SUM(latency_sum), SUM(latency_sq_sum), "
    "MIN(min_latency), MAX(max_latency)"
)


def _counts(row: tuple[Any, ...]) -> Counts:
    ok, fail, unknown, lat_count, lat_sum, lat_sq, lat_min, lat_max = row
    return Counts(int(ok or 0), int(fail or 0), int(unknown or 0), int(lat_count or 0), float(lat_sum or 0),
                  float(lat_sq or 0), lat_min, lat_max)


def _server_filter(server_id: int | None) -> tuple[str, list[Any]]:
    return (" AND server_id = ?", [server_id]) if server_id is not None else ("", [])


def raw_counts(since: int, until: int | None = None, server_id: int | None = None) -> dict[int, Counts]:
    where, params = _server_filter(server_id)
    sql = f"SELECT server_id, {_COUNT_COLUMNS} FROM checks WHERE checked_at >= ?"
    args: list[Any] = [since]
    if until is not None:
        sql += " AND checked_at < ?"
        args.append(until)
    cursor = db.execute_sql(sql + where + " GROUP BY server_id", args + params)
    return {row[0]: _counts(row[1:]) for row in cursor.fetchall()}


def hourly_counts(since_hour: int, until_hour: int, server_id: int | None = None) -> dict[int, Counts]:
    if until_hour <= since_hour:
        return {}
    where, params = _server_filter(server_id)
    cursor = db.execute_sql(
        f"SELECT server_id, {_HOURLY_COLUMNS} FROM hourly_stats WHERE hour >= ? AND hour < ?{where} "
        "GROUP BY server_id", [since_hour, until_hour] + params)
    return {row[0]: _counts(row[1:]) for row in cursor.fetchall()}


def aggregated_until() -> int:
    return int(settings.get("agg_until") or 0)


def window_counts(since: int, now: int, server_id: int | None = None) -> dict[int, Counts]:
    """Counts over [since, now]: raw only when available, else hourly + recent raw."""
    raw_start = now - int(settings.get("raw_retention_hours")) * HOUR
    if since >= raw_start:
        return raw_counts(since, server_id=server_id)
    boundary = max(aggregated_until(), hour_floor(since))
    result = hourly_counts(hour_floor(since), boundary, server_id)
    for sid, counts in raw_counts(max(boundary, since), server_id=server_id).items():
        result.setdefault(sid, Counts()).add(counts)
    return result


def series_rows(since: int, server_id: int | None = None) -> list[tuple[int, int, Counts]]:
    """(server_id, slot_start, Counts) rows: hourly before ``agg_until``, raw per 30 min after."""
    boundary = max(aggregated_until(), hour_floor(since))
    where, params = _server_filter(server_id)
    rows: list[tuple[int, int, Counts]] = []
    if boundary > hour_floor(since):
        cursor = db.execute_sql(
            f"SELECT server_id, hour, {_HOURLY_COLUMNS} FROM hourly_stats WHERE hour >= ? AND hour < ?{where} "
            "GROUP BY server_id, hour", [hour_floor(since), boundary] + params)
        rows += [(r[0], r[1], _counts(r[2:])) for r in cursor.fetchall()]
    cursor = db.execute_sql(
        f"SELECT server_id, (checked_at / 1800) * 1800 AS slot, {_COUNT_COLUMNS} FROM checks "
        f"WHERE checked_at >= ?{where} GROUP BY server_id, slot", [max(boundary, since)] + params)
    rows += [(r[0], r[1], _counts(r[2:])) for r in cursor.fetchall()]
    return rows


def bucketize(buckets: list[Bucket], rows: Iterable[tuple[int, int, Counts]]) -> dict[int, list[Counts]]:
    starts = [b.start for b in buckets]
    out: dict[int, list[Counts]] = {}
    for server_id, ts, counts in rows:
        index = bisect_right(starts, ts) - 1
        if 0 <= index < len(buckets) and ts < buckets[index].end:
            out.setdefault(server_id, [Counts() for _ in buckets])[index].add(counts)
    return out


def bar_counts(range_key: str, buckets: list[Bucket], server_id: int | None = None) -> dict[int, list[Counts]]:
    start = buckets[0].start
    if range_key == "24h":  # exact 30-minute buckets from raw data
        size = buckets[0].end - buckets[0].start
        where, params = _server_filter(server_id)
        cursor = db.execute_sql(
            f"SELECT server_id, ? + ((checked_at - ?) / ?) * ? AS slot, {_COUNT_COLUMNS} FROM checks "
            f"WHERE checked_at >= ?{where} GROUP BY server_id, slot", [start, start, size, size, start] + params)
        rows = [(r[0], r[1], _counts(r[2:])) for r in cursor.fetchall()]
    else:
        rows = series_rows(start, server_id)
    return bucketize(buckets, rows)


def bar_items(buckets: list[Bucket], counts: list[Counts], incident_starts: list[int]) -> list[dict[str, Any]]:
    """Status bar cells; the public list and the history API use this same function."""
    return [{
        "level": bar_level(c.uptime), "uptime": _round(c.uptime, 2), "checks": c.valid,
        "incidents": sum(bucket.start <= s < bucket.end for s in incident_starts),
    } for bucket, c in zip(buckets, counts)]


def incidents_overlapping(since: int, now: int, server_id: int | None = None) -> list[Incident]:
    query = Incident.select().where(Incident.started_at < now,
                                    Incident.ended_at.is_null() | (Incident.ended_at >= since))
    if server_id is not None:
        query = query.where(Incident.server == server_id)
    return list(query.order_by(Incident.started_at.desc()))


def incident_stats(since: int, now: int, server_id: int | None = None) -> dict[int, IncidentStats]:
    result: dict[int, IncidentStats] = {}
    for inc in incidents_overlapping(since, now, server_id):
        st = result.setdefault(inc.server_id, IncidentStats())
        end = inc.ended_at or now
        st.count += 1
        st.downtime += max(0, min(end, now) - max(inc.started_at, since))
        st.longest = max(st.longest, end - inc.started_at)
        if inc.ended_at is not None:
            st._closed.append(inc.ended_at - inc.started_at)
    for st in result.values():
        st.mttr = sum(st._closed) / len(st._closed) if st._closed else None
    return result


# --------------------------------------------------------------- service

class StatsService:
    """Computes metrics; results are cached (≤60s) and invalidated each cycle."""

    def __init__(self, live: LiveRegistry, in_incident: Callable[[int], bool]) -> None:
        self.live = live
        self.in_incident = in_incident
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._locks: dict[tuple[Any, ...], threading.Lock] = {}
        self._guard = threading.Lock()

    def invalidate(self) -> None:
        with self._guard:
            self._cache.clear()

    def _cached(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
        with self._guard:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # one computation per key; others wait for it
            with self._guard:
                hit = self._cache.get(key)
                if hit and hit[0] > time.monotonic():
                    return hit[1]
            value = compute()
            with self._guard:
                self._cache[key] = (time.monotonic() + CACHE_TTL, value)
            return value

    # ----------------------------------------------------------- status
    def current_status(self, server_id: int, now: int | None = None) -> str:
        return classify_status(
            self.live.points(server_id), self.in_incident(server_id), now or now_ts(),
            interval=settings.get("check_interval"), degraded_latency_ms=settings.get("degraded_latency_ms"),
            degraded_fail_percent=settings.get("degraded_fail_percent"),
        )

    def live_metrics(self, server_id: int, now: int) -> ServerMetrics:
        m = ServerMetrics(server_id, status=self.current_status(server_id, now))
        last = self.live.last_valid(server_id)
        if last:
            m.last_check_at = last.ts
            m.latency_ms = last.latency_ms if last.status == "ok" else None
            m.last_error = last.error_type if last.status == "fail" else None
        recent = self.live.recent_latencies(server_id, now - PING_WINDOW)
        m.ping_ms = _round(median(recent), 0) if recent else m.latency_ms
        latencies = self.live.recent_latencies(server_id, now - HOUR)
        m.median_1h = median(latencies)
        m.avg_1h = sum(latencies) / len(latencies) if latencies else None
        return m

    def server_metrics(self) -> dict[int, ServerMetrics]:
        return self._cached(("metrics",), self._compute_metrics)

    def _compute_metrics(self) -> dict[int, ServerMetrics]:
        now = now_ts()
        names = dict(Server.select(Server.id, Server.name).where(Server.is_active == True).tuples())  # noqa: E712
        c24 = window_counts(now - RANGES["24h"], now)
        c7 = window_counts(now - RANGES["7d"], now)
        c30 = window_counts(now - RANGES["30d"], now)
        inc24 = incident_stats(now - DAY, now)
        result: dict[int, ServerMetrics] = {}
        for sid, name in names.items():
            m = self.live_metrics(sid, now)
            m.name = name
            day = c24.get(sid, Counts())
            m.uptime_24h = day.uptime
            m.uptime_7d = c7.get(sid, Counts()).uptime
            m.uptime_30d = c30.get(sid, Counts()).uptime
            m.valid_24h = day.valid
            m.jitter_24h = day.jitter
            m.stability = stability_score(day.uptime, day.jitter) if not m.collecting else None
            m.incidents_24h = inc24[sid].count if sid in inc24 else 0
            result[sid] = m
        return result

    # ----------------------------------------------------- public status
    def public_status(self, range_key: str) -> dict[str, Any]:
        range_key = range_key if range_key in RANGES else "24h"
        return self._cached(("status", range_key), lambda: self._compute_public_status(range_key))

    def _compute_public_status(self, range_key: str) -> dict[str, Any]:
        now = now_ts()
        tz = settings.tz
        servers = list(Server.select().where(Server.is_active == True, Server.is_public == True)  # noqa: E712
                       .order_by(Server.sort_order, Server.id))
        metrics = self.server_metrics()
        buckets = make_buckets(range_key, now, tz)
        bars = bar_counts(range_key, buckets)
        incident_starts: dict[int, list[int]] = {}
        for inc in incidents_overlapping(buckets[0].start, now):
            incident_starts.setdefault(inc.server_id, []).append(inc.started_at)
        last_by_server: dict[int, Incident] = {}
        for inc in incidents_overlapping(now - 30 * DAY, now):
            last_by_server.setdefault(inc.server_id, inc)
        bucket_info = [{
            "start": iso_utc(b.start), "end": iso_utc(b.end),
            "label": fmt_date(b.start, tz) if range_key == "30d" else fmt_range(b.start, b.end, tz),
        } for b in buckets]
        cards = []
        for server in servers:
            m = metrics.get(server.id) or self.live_metrics(server.id, now)
            counts = bars.get(server.id) or [Counts() for _ in buckets]
            bucket_items = bar_items(buckets, counts, incident_starts.get(server.id, []))
            cards.append({
                "id": server.id, "name": server.name, "country": server.country,
                "protocol": server.protocol, "transport": server.transport,
                "tags": server.tag_list, "status": m.status, "status_label": display_label(m.status, m.collecting),
                "latency_ms": m.latency_ms, "ping_ms": m.ping_ms, "uptime_24h": _round(m.uptime_24h, 2),
                "uptime_7d": _round(m.uptime_7d, 2), "uptime_30d": _round(m.uptime_30d, 2),
                "median_1h": _round(m.median_1h, 0), "avg_1h": _round(m.avg_1h, 0),
                "stability": m.stability, "collecting": m.collecting,
                "last_check_at": iso_utc(m.last_check_at) if m.last_check_at else None,
                "last_check_label": fmt_datetime(m.last_check_at, tz) if m.last_check_at else None,
                "last_incident": _last_incident(last_by_server.get(server.id), now),
                "bars": bucket_items,
            })
        public_metrics = [metrics[c["id"]] for c in cards if c["id"] in metrics]
        uptimes = [c["uptime_24h"] for c in cards if c["uptime_24h"] is not None]
        last_check = self.live.last_check_ts()
        return {
            "generated_at": iso_utc(now),
            "generated_at_label": fmt_datetime(now, tz),
            "last_check_at": iso_utc(last_check) if last_check else None,
            "last_check_label": fmt_datetime(last_check, tz) if last_check else None,
            "range": range_key,
            "range_label": range_key,
            "timezone": {"name": tz, "offset": tz_offset(tz, now)},
            "buckets": bucket_info,
            "site": {
                "title": settings.get("site_title"), "description": settings.get("site_description"),
                "location": settings.get("test_location"),
            },
            "summary": {
                "total": len(cards),
                "online": sum(c["status"] == ONLINE for c in cards),
                "degraded": sum(c["status"] == DEGRADED for c in cards),
                "offline": sum(c["status"] == OFFLINE for c in cards),
                "unknown": sum(c["status"] == UNKNOWN for c in cards),
                "avg_uptime_24h": _round(sum(uptimes) / len(uptimes), 2) if uptimes else None,
            },
            "servers": cards,
            "order": {"fastest": rank_fastest(public_metrics), "stable": rank_stable(public_metrics)},
        }

    # ----------------------------------------------------------- history
    def server_history(self, server_id: int, range_key: str) -> dict[str, Any]:
        range_key = range_key if range_key in RANGES else "24h"
        return self._cached(("history", server_id, range_key),
                            lambda: self._compute_history(server_id, range_key))

    def _compute_history(self, server_id: int, range_key: str) -> dict[str, Any]:
        now = now_ts()
        tz = settings.tz
        server = Server.get_by_id(server_id)
        step = HISTORY_STEP[range_key]
        buckets = fixed_buckets(now, step, RANGES[range_key] // step, tz)
        if range_key == "24h":
            cursor = db.execute_sql(
                f"SELECT server_id, (checked_at / 300) * 300 AS slot, {_COUNT_COLUMNS} FROM checks "
                "WHERE checked_at >= ? AND server_id = ? GROUP BY slot", [buckets[0].start, server_id])
            rows = [(r[0], r[1], _counts(r[2:])) for r in cursor.fetchall()]
        else:
            rows = series_rows(buckets[0].start, server_id)
        series = bucketize(buckets, rows).get(server_id) or [Counts() for _ in buckets]
        bar_buckets = make_buckets(range_key, now, tz)
        bar_counts_ = bar_counts(range_key, bar_buckets, server_id).get(server_id) or [Counts() for _ in bar_buckets]
        bar_starts = [i.started_at for i in incidents_overlapping(bar_buckets[0].start, now, server_id)]
        bars = [{"start": iso_utc(b.start), "end": iso_utc(b.end), **item}
                for b, item in zip(bar_buckets, bar_items(bar_buckets, bar_counts_, bar_starts))]
        points = [{
            "t": iso_utc(b.start), "label": fmt_datetime(b.start, tz),
            "latency": _round(c.avg_latency, 0), "uptime": _round(c.uptime, 2), "checks": c.valid,
        } for b, c in zip(buckets, series)]

        since = now - RANGES[range_key]
        total = window_counts(since, now, server_id).get(server_id, Counts())
        p95 = None
        if range_key == "24h":
            latencies = [r[0] for r in db.execute_sql(
                "SELECT latency_ms FROM checks WHERE server_id = ? AND checked_at >= ? AND latency_ms IS NOT NULL",
                [server_id, since]).fetchall()]
            p95 = percentile(latencies, 95)
        stats = incident_stats(since, now, server_id).get(server_id, IncidentStats())
        incidents = incidents_overlapping(since, now, server_id)[:20]
        m = self.server_metrics().get(server_id) or self.live_metrics(server_id, now)
        return {
            "server": {
                "id": server.id, "name": server.name, "country": server.country,
                "protocol": server.protocol, "transport": server.transport,
                "status": m.status, "status_label": display_label(m.status, m.collecting),
                "collecting": m.collecting,
                "latency_ms": m.latency_ms, "median_1h": _round(m.median_1h, 0), "stability": m.stability,
            },
            "range": range_key,
            "range_label": range_key,
            "timezone": {"name": tz, "offset": tz_offset(tz, now)},
            "points": points,
            "bars": bars,
            "summary": {
                "uptime": _round(total.uptime, 2), "checks": total.valid, "failures": total.fail,
                "avg_latency": _round(total.avg_latency, 0), "min_latency": total.lat_min,
                "max_latency": total.lat_max, "p95_latency": p95, "jitter": _round(total.jitter, 0),
            },
            "incident_stats": {
                "count": stats.count, "downtime": stats.downtime, "downtime_label": fmt_duration(stats.downtime),
                "mttr_label": fmt_duration(stats.mttr) if stats.mttr is not None else "—",
                "longest_label": fmt_duration(stats.longest) if stats.count else "—",
            },
            "incidents": [{
                "started_at": iso_utc(i.started_at),
                "ended_at": iso_utc(i.ended_at) if i.ended_at else None,
                "start_label": fmt_datetime(i.started_at, tz),
                "end_label": fmt_datetime(i.ended_at, tz) if i.ended_at else None,
                "duration_label": fmt_duration((i.ended_at or now) - i.started_at),
                "error_label": error_label(i.error_type), "ongoing": i.ended_at is None,
            } for i in incidents],
        }


def _last_incident(inc: Incident | None, now: int) -> dict[str, Any] | None:
    if inc is None:
        return None
    return {
        "started_at": iso_utc(inc.started_at),
        "ended_at": iso_utc(inc.ended_at) if inc.ended_at else None,
        "duration_seconds": max(0, int((inc.ended_at or now) - inc.started_at)),
    }


def _round(value: float | None, digits: int) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(value, digits)
