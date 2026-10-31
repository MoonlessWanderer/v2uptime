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


def test_rank_fastest_includes_a_new_server_with_a_ping():
    metrics = [m(1, median=300), m(2, median=100), m(3, status=DEGRADED, median=50),
               m(4, median=10, valid=3), m(5, status=OFFLINE, median=5),
               m(6, status=UNKNOWN, median=8, valid=1),
               m(7, status=UNKNOWN, median=1, valid=20)]
    assert rank_fastest(metrics) == [6, 4, 2, 1]


def test_rank_stable_uses_a_score_from_a_new_server():
    metrics = [m(1, stability=80), m(2, stability=95, status=DEGRADED), m(3, stability=99, valid=2),
               m(4, stability=None, valid=1)]
    assert rank_stable(metrics) == [3, 2, 1]


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
    # Newest check is at `now`, so it stays inside the half-hour that is still open.
    # A start of now-1200 puts that check in the previous cell for the first minute of each half-hour.
    add_checks(visible, now - 19 * 60, "o" * 20)
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
    assert len(card["hour"]) == 60
    assert card["hour"][0] == {"level": "none", "at": None, "status": None, "ms": None}
    assert sum(cell["at"] is not None for cell in card["hour"]) == 20
    assert card["hour"][-1]["level"] == "up" and card["hour"][-1]["status"] == "ok"
    assert payload["summary"]["online"] == 1
    assert payload["order"]["fastest"] == [visible.id]
    assert hidden.id not in payload["order"]["stable"] and inactive.id not in payload["order"]["stable"]


def test_hour_is_the_last_60_checks_oldest_first(database):
    from app.models import Check
    from app.timeutil import iso_utc

    now = now_ts()
    long = make_server("Long", port=20111)
    tied = make_server("Tied", port=20112)
    add_checks(long, now - 61, "o" * 61, step=1, latency=10)
    Check.create(server=tied, checked_at=now - 1, status="unknown", latency_ms=None)
    Check.create(server=tied, checked_at=now, status="ok", latency_ms=125)
    Check.create(server=tied, checked_at=now, status="fail", latency_ms=None)
    stats = StatsService(LiveRegistry(), lambda sid: False)
    cards = {card["name"]: card for card in stats.public_status("24h")["servers"]}

    hour = cards["Long"]["hour"]
    assert len(hour) == 60 and all(cell["at"] for cell in hour)
    assert hour[0]["at"] == iso_utc(now - 60) and hour[-1]["at"] == iso_utc(now - 1)
    assert len(cards["Long"]["bars"]) == 48

    hour = cards["Tied"]["hour"]
    assert hour[:57] == [{"level": "none", "at": None, "status": None, "ms": None}] * 57
    assert hour[57] == {"level": "none", "at": iso_utc(now - 1), "status": "unknown", "ms": None}
    assert hour[58] == {"level": "up", "at": iso_utc(now), "status": "ok", "ms": 125}
    assert hour[59] == {"level": "down", "at": iso_utc(now), "status": "fail", "ms": None}


def test_24h_point_is_the_median_ping_of_its_slot(database):
    from app.models import Check
    from app.timeutil import iso_utc

    now = now_ts()
    slot = (now // 1800) * 1800 - 1800  # the previous half-hour, same slot as the status bar
    server = make_server("Median", port=20221)
    for offset, latency in ((0, 100), (60, 100), (120, 900), (180, None)):
        Check.create(server=server, checked_at=slot + offset, status="ok" if latency else "fail", latency_ms=latency)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(server.id, "24h")
    assert [item["t"] for item in history["points"]] == [cell["start"] for cell in history["bars"]]
    point = next(item for item in history["points"] if item["t"] == iso_utc(slot))
    cell = next(item for item in history["bars"] if item["start"] == iso_utc(slot))
    assert point["latency"] == 100  # mean of 100, 100, 900 would be 367; the failed check has no ping
    assert point["uptime"] == cell["uptime"]


def test_1h_point_is_the_same_slot_as_its_bar_cell(database):
    from app.models import Check
    from app.timeutil import iso_utc

    now = now_ts()
    slot = (now // 60) * 60 - 60
    server = make_server("Hour", port=20222)
    for offset, latency in ((0, 80), (15, 80), (30, 400)):
        Check.create(server=server, checked_at=slot + offset, status="ok", latency_ms=latency)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(server.id, "1h")
    assert [item["t"] for item in history["points"]] == [cell["start"] for cell in history["bars"]]
    point = next(item for item in history["points"] if item["t"] == iso_utc(slot))
    cell = next(item for item in history["bars"] if item["start"] == iso_utc(slot))
    assert point["latency"] == 80  # mean of 80, 80, 400 would be 187
    assert point["uptime"] == cell["uptime"]


def test_1h_drops_the_open_minute_until_a_check_lands_in_it(database):
    from app.models import Check
    from app.timeutil import iso_utc

    now = now_ts()
    minute = (now // 60) * 60
    waiting = make_server("Waiting", port=20223)
    Check.create(server=waiting, checked_at=minute - 20, status="ok", latency_ms=55)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(waiting.id, "1h")
    assert len(history["points"]) == 60 == len(history["bars"])
    assert history["bars"][-1]["end"] == iso_utc(minute)
    assert history["points"][-1]["latency"] == 55
    assert history["points"][-1]["checks"] == 1
    assert history["points"][-2]["checks"] == 0

    landed = make_server("Landed", port=20224)
    Check.create(server=landed, checked_at=now, status="ok", latency_ms=42)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(landed.id, "1h")
    assert history["bars"][-1]["end"] == iso_utc(minute + 60)
    assert history["points"][-1]["latency"] == 42
    assert history["points"][-1]["checks"] == 1

    # Two checks in an earlier minute, and nothing since: the chart ends on
    # that minute, not on the empty minute after it.
    both = make_server("Both", port=20225)
    Check.create(server=both, checked_at=minute - 110, status="ok", latency_ms=30)
    Check.create(server=both, checked_at=minute - 80, status="ok", latency_ms=50)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(both.id, "1h")
    assert history["bars"][-1]["start"] == iso_utc(minute - 120)
    assert history["bars"][-1]["end"] == iso_utc(minute - 60)
    assert history["points"][-1]["checks"] == 2
    assert history["points"][-1]["latency"] == 40

    # Nothing recorded: keep the clock hour, including the minute that is still open.
    empty = make_server("Empty", port=20226)
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(empty.id, "1h")
    assert len(history["points"]) == 60
    assert history["bars"][-1]["end"] == iso_utc(minute + 60)

    # A failure and an unknown result are checks. They stay on the newest minute.
    failed = make_server("Failed", port=20227)
    Check.create(server=failed, checked_at=minute - 20, status="ok", latency_ms=70)
    Check.create(server=failed, checked_at=now, status="fail", latency_ms=None, error_type="timeout")
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(failed.id, "1h")
    assert history["bars"][-1]["start"] == iso_utc(minute)
    assert history["points"][-1]["checks"] == 1
    assert history["points"][-1]["latency"] is None

    outage = make_server("Outage", port=20228)
    Check.create(server=outage, checked_at=minute - 200, status="ok", latency_ms=80)
    Check.create(server=outage, checked_at=minute - 90, status="unknown", latency_ms=None, error_type="network")
    history = StatsService(LiveRegistry(), lambda sid: False).server_history(outage.id, "1h")
    assert history["bars"][-1]["start"] == iso_utc(minute - 120)
    assert history["points"][-1]["uptime"] is None
    assert history["points"][-1]["checks"] == 0


def test_a_new_server_sorts_once_it_has_a_ping(database):
    now = now_ts()
    fast = make_server("Fast", port=20231)
    slow = make_server("Slow", port=20232)
    once = make_server("Once", port=20233)
    add_checks(fast, now - 120, "oo", latency=40)
    add_checks(slow, now - 120, "oo", latency=200)
    add_checks(once, now - 60, "o", latency=15)
    live = LiveRegistry()
    live.add(fast.id, LivePoint(now - 120, "ok", 40))
    live.add(fast.id, LivePoint(now - 60, "ok", 40))
    live.add(slow.id, LivePoint(now - 120, "ok", 200))
    live.add(slow.id, LivePoint(now - 60, "ok", 200))
    live.add(once.id, LivePoint(now - 60, "ok", 15))
    stats = StatsService(live, lambda sid: False)
    order = stats.public_status("24h")["order"]
    assert order["fastest"] == [once.id, fast.id, slow.id]
    assert order["stable"] == [fast.id, slow.id]
    metrics = stats.server_metrics()
    assert metrics[once.id].stability is None
    assert metrics[fast.id].stability is not None


def test_history_payload(database):
    now = now_ts()
    server = make_server()
    add_checks(server, now - 3000, "oooff" * 4)
    Incident.create(server=server, started_at=now - 2000, ended_at=now - 1000, duration_seconds=1000)
    live = LiveRegistry()
    stats = StatsService(live, lambda sid: False)
    for key, count in (("1h", 60), ("24h", 48), ("7d", 42), ("30d", 30)):
        history = stats.server_history(server.id, key)
        assert len(history["points"]) == count == len(history["bars"])
        assert [item["t"] for item in history["points"]] == [cell["start"] for cell in history["bars"]]
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
