import pytest

from app.services.incidents import IncidentService
from app.services.stats import (
    Counts,
    ServerMetrics,
    StatsService,
    bar_level,
    day_buckets,
    fixed_buckets,
    incident_stats,
    rank_fastest,
    rank_stable,
    stability_score,
    uptime_percent,
)
from app.services.status import (
    DEGRADED,
    OFFLINE,
    ONLINE,
    UNKNOWN,
    LivePoint,
    LiveRegistry,
    classify_status,
    seed_rows,
)
from app.models import Incident
from app.timeutil import DAY, now_ts

from tests.conftest import add_checks, make_server


def test_uptime_ignores_unknown():
    assert uptime_percent(0, 0) is None
    assert uptime_percent(9, 1) == 90.0


def test_stability_score_formula():
    assert stability_score(100, 0) == 100.0
    assert stability_score(100, 500) == 70.0  # jitter >= reference -> no jitter credit
    assert stability_score(90, 250) == pytest.approx((0.9 * 0.7 + 0.5 * 0.3) * 100)
    assert stability_score(None, 10) is None


def test_counts_jitter():
    c = Counts(ok=2, lat_count=2, lat_sum=300, lat_sq=100 ** 2 + 200 ** 2)
    assert c.jitter == pytest.approx(50.0)
    assert Counts(ok=1, lat_count=1, lat_sum=10, lat_sq=100).jitter is None


def m(sid, status=ONLINE, median=100.0, stability=90.0, valid=100):
    return ServerMetrics(sid, status=status, median_1h=median, stability=stability, valid_24h=valid)


def test_rank_fastest_only_online_with_data():
    metrics = [m(1, median=300), m(2, median=100), m(3, status=DEGRADED, median=50),
               m(4, median=10, valid=3), m(5, status=OFFLINE, median=5)]
    assert rank_fastest(metrics) == [2, 1]


def test_rank_stable_excludes_collecting():
    metrics = [m(1, stability=80), m(2, stability=95, status=DEGRADED), m(3, stability=99, valid=2),
               m(4, stability=None)]
    assert rank_stable(metrics) == [2, 1]


def test_bar_levels():
    assert [bar_level(v) for v in (None, 100, 97, 85, 10)] == ["none", "up", "minor", "major", "down"]


def test_buckets_are_aligned_and_contiguous():
    now = 1_761_900_000
    buckets = fixed_buckets(now, 1800, 48, "Asia/Tehran")
    assert len(buckets) == 48 and buckets[-1].start <= now < buckets[-1].end
    assert all(a.end == b.start for a, b in zip(buckets, buckets[1:]))
    days = day_buckets(now, 30, "Asia/Tehran")
    assert len(days) == 30 and days[-1].start <= now < days[-1].end
    assert all(b.end - b.start == DAY for b in days)  # Iran has no DST
    four_hours = fixed_buckets(now, 4 * 3600, 42, "Asia/Tehran")
    # aligned to local (UTC+3:30) 4h boundaries
    assert (four_hours[0].start + 3 * 3600 + 1800) % (4 * 3600) == 0


def P(ts, status, latency=100):
    return LivePoint(ts, status, latency if status == "ok" else None)


def classify(points, in_incident=False, now=10_000):
    return classify_status(points, in_incident, now, interval=60, degraded_latency_ms=1500,
                           degraded_fail_percent=20)


def test_status_rules():
    now = 10_000
    assert classify([P(now - 60, "ok"), P(now, "ok")]) == ONLINE
    assert classify([P(now - 60, "ok"), P(now, "ok", 2000)]) == DEGRADED  # slow
    assert classify([P(now - 60, "ok"), P(now, "fail")]) == DEGRADED  # failing but no incident yet
    ten_min = [P(now - 540 + i * 60, "fail" if i < 3 else "ok") for i in range(10)]
    assert classify(ten_min) == DEGRADED  # 30% failures in 10 minutes
    assert classify([P(now, "ok")]) == UNKNOWN  # not enough data
    assert classify([P(now - 1000, "ok"), P(now - 900, "ok")]) == UNKNOWN  # stale
    assert classify([P(now - 60, "ok"), P(now, "unknown"), P(now, "unknown")]) == UNKNOWN
    assert classify([P(now, "ok"), P(now, "ok")], in_incident=True) == OFFLINE


def test_incident_stats_clip_to_window(database):
    server = make_server()
    Incident.create(server=server, started_at=500, ended_at=1500, duration_seconds=1000)
    Incident.create(server=server, started_at=2000, ended_at=2100, duration_seconds=100)
    Incident.create(server=server, started_at=2500)  # ongoing
    st = incident_stats(since=1000, now=3000)[server.id]
    assert st.count == 3
    assert st.downtime == 500 + 100 + 500
    assert st.mttr == 550 and st.longest == 1000


def test_public_status_hides_sensitive_and_hidden(database):
    now = now_ts()
    visible = make_server("Visible", address="secret-host.example.com", port=20001)
    hidden = make_server("Hidden", public=False, port=20002)
    inactive = make_server("Inactive", active=False, port=20003)
    add_checks(visible, now - 1200, "o" * 20)
    live = LiveRegistry()
    live.seed(seed_rows(now - 3600))
    incidents = IncidentService()
    stats = StatsService(live, incidents.in_incident)
    payload = stats.public_status("24h")
    assert [s["name"] for s in payload["servers"]] == ["Visible"]
    text = repr(payload)
    for secret in ("secret-host", "vless://", "20001", "address", "raw_link"):
        assert secret not in text
    card = payload["servers"][0]
    assert card["status"] == ONLINE and card["uptime_24h"] == 100
    assert len(card["bars"]) == 48 and card["bars"][-1]["level"] == "up"
    assert payload["summary"]["online"] == 1
    assert payload["order"]["fastest"] == [visible.id]
    assert hidden.id not in payload["order"]["stable"] and inactive.id not in payload["order"]["stable"]


def test_history_payload(database):
    now = now_ts()
    server = make_server()
    add_checks(server, now - 3000, "oooff" * 4)
    Incident.create(server=server, started_at=now - 2000, ended_at=now - 1000, duration_seconds=1000)
    live = LiveRegistry()
    stats = StatsService(live, lambda sid: False)
    for key, count in (("24h", 288), ("7d", 168), ("30d", 120)):
        history = stats.server_history(server.id, key)
        assert len(history["points"]) == count
    history = stats.server_history(server.id, "24h")
    assert history["summary"]["uptime"] == 60.0
    assert history["incident_stats"]["count"] == 1
    assert history["incidents"][0]["ongoing"] is False
    assert history["incidents"][0]["started_at"].endswith("Z")
    assert history["incidents"][0]["ended_at"].endswith("Z")
    assert history["points"][0]["t"].endswith("Z")
    assert history["range_label"] == "24h"


def test_public_ping_is_the_10_minute_median(database):
    live = LiveRegistry()
    now = now_ts()
    for ts, latency in ((now - 1200, 900), (now - 300, 100), (now - 200, 300), (now - 100, 120)):
        live.add(1, LivePoint(ts, "ok", latency))
    stats = StatsService(live, lambda _id: False)
    metrics = stats.live_metrics(1, now)
    assert metrics.ping_ms == 120 and metrics.latency_ms == 120  # median of 100/300/120; 900 is too old
    quiet = LiveRegistry()
    quiet.add(2, LivePoint(now - 900, "ok", 250))  # nothing in the last 10 minutes: keep the last value
    assert StatsService(quiet, lambda _id: False).live_metrics(2, now).ping_ms == 250


def test_connect_ms_column_is_added_to_old_databases(tmp_path):
    from app.db import init_db

    path = tmp_path / "old.db"
    database = init_db(path)
    database.execute_sql("ALTER TABLE checks DROP COLUMN connect_ms")
    assert "connect_ms" not in {c.name for c in database.get_columns("checks")}
    database.close()
    database = init_db(path)
    assert "connect_ms" in {c.name for c in database.get_columns("checks")}
    database.close()
