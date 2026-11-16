from app.models import Check, HourlyStat, Incident, TelegramOutbox
from app.services.aggregator import aggregate_hour, cleanup, percentile, run_aggregation
from app.services.settings import settings
from app.services.stats import window_counts
from app.timeutil import DAY, HOUR

from tests.conftest import add_checks, make_server

H0 = 1_700_000_000 - 1_700_000_000 % HOUR  # an hour boundary


def test_aggregate_hour_values(database):
    server = make_server()
    add_checks(server, H0, "ooofu", latency=lambda i: 100 * (i + 1))  # ok latencies 100,200,300
    assert aggregate_hour(H0) == 1
    row = HourlyStat.get()
    assert (row.total, row.success, row.fail, row.unknown) == (5, 3, 1, 1)
    assert row.latency_count == 3 and row.latency_sum == 600 and row.latency_sq_sum == 140000
    assert (row.min_latency, row.max_latency, row.avg_latency, row.p95_latency) == (100, 300, 200, 300)


def test_aggregation_is_idempotent(database):
    server = make_server()
    add_checks(server, H0, "oof")
    aggregate_hour(H0)
    aggregate_hour(H0)
    assert HourlyStat.select().count() == 1


def test_catch_up_after_downtime(database):
    server = make_server()
    for h in range(5):
        add_checks(server, H0 + h * HOUR, "oo")
    hours = run_aggregation(now=H0 + 5 * HOUR + 10)
    assert hours == [H0 + h * HOUR for h in range(5)]
    assert settings.get("agg_until") == H0 + 5 * HOUR
    # the current (incomplete) hour is not aggregated
    add_checks(server, H0 + 5 * HOUR, "o")
    assert HourlyStat.select().where(HourlyStat.hour == H0 + 5 * HOUR).count() == 0
    # next run re-processes only the last aggregated hour + new ones
    assert run_aggregation(now=H0 + 6 * HOUR + 5) == [H0 + 4 * HOUR, H0 + 5 * HOUR]


def test_long_windows_combine_hourly_and_unaggregated_raw(database):
    server = make_server()
    now = H0 + 40 * HOUR
    add_checks(server, now - 30 * HOUR, "of", step=60)  # older than 24h, aggregated below
    run_aggregation(now=now - 20 * HOUR)  # agg_until = now - 20h
    Check.delete().where(Check.checked_at < now - 26 * HOUR).execute()  # raw already pruned
    add_checks(server, now - 10 * HOUR, "ooo")  # not aggregated yet
    week = window_counts(now - 7 * DAY, now)[server.id]
    assert (week.ok, week.fail) == (4, 1)
    day = window_counts(now - DAY, now)[server.id]  # 24h: raw only
    assert (day.ok, day.fail) == (3, 0)


def test_unknown_is_excluded_from_uptime(database):
    server = make_server()
    add_checks(server, H0, "oouuuuf")
    counts = window_counts(H0 - 10, H0 + HOUR)[server.id]
    assert counts.uptime == 2 / 3 * 100


def test_cleanup_respects_retention(database):
    server = make_server()
    now = H0 + 100 * DAY
    add_checks(server, now - 49 * HOUR, "o")
    add_checks(server, now - 47 * HOUR, "o")
    HourlyStat.create(server=server, hour=now - 91 * DAY)
    HourlyStat.create(server=server, hour=now - 89 * DAY)
    Incident.create(server=server, started_at=now - 95 * DAY, ended_at=now - 94 * DAY)
    TelegramOutbox.create(event_type="x", chat_id="1", text="t", created_at=now - 8 * DAY, status="sent")
    TelegramOutbox.create(event_type="x", chat_id="1", text="t", created_at=now - 8 * DAY, status="pending")
    removed = cleanup(now=now)
    assert removed == {"checks": 1, "hourly": 1, "incidents": 1, "outbox": 1}


def test_percentile():
    assert percentile([], 95) is None
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([5], 95) == 5
