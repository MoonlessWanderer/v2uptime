from datetime import datetime, timezone

import pytest

from app.db import init_db
from app.models import Setting
from app.services.settings import DEFINITIONS, WEEKDAY_CHOICES, settings
from app.timeutil import (
    fmt_ago,
    fmt_datetime,
    fmt_duration,
    fmt_percent,
    fmt_range,
    fmt_time,
    iso_utc,
    tz_label,
    tz_offset,
)

TEHRAN = "Asia/Tehran"
# 2026-10-31 14:58:05 UTC == 2026-10-31 18:28:05 in Asia/Tehran.
TS = int(datetime(2026, 10, 31, 14, 58, 5, tzinfo=timezone.utc).timestamp())


def test_utc_converts_to_tehran():
    assert fmt_datetime(TS, TEHRAN) == "2026-10-31 18:28"
    assert fmt_datetime(TS, TEHRAN, seconds=True) == "2026-10-31 18:28:05"
    assert fmt_time(TS, TEHRAN) == "18:28"
    assert tz_offset(TEHRAN, TS) == "+03:30"
    assert tz_label(TEHRAN, TS) == "UTC+03:30"
    assert fmt_range(TS, TS + 1800, TEHRAN) == "2026-10-31 18:28–18:58"
    assert fmt_datetime(None, TEHRAN) == "—"


def test_fmt_duration_boundaries():
    assert fmt_duration(45) == "45s"
    assert fmt_duration(59) == "59s"
    assert fmt_duration(60) == "1m"
    assert fmt_duration(12 * 60) == "12m"
    assert fmt_duration(3599) == "59m 59s"
    assert fmt_duration(3600) == "1h"
    assert fmt_duration(2 * 3600 + 15 * 60) == "2h 15m"
    assert fmt_duration(86400) == "1d"
    assert fmt_duration(3 * 86400 + 4 * 3600) == "3d 4h"
    assert fmt_duration(None) == "—"


def test_fmt_ago():
    now = 1_700_000_000
    assert fmt_ago(now, now) == "just now"
    assert fmt_ago(now - 30, now) == "just now"
    assert fmt_ago(now - 5 * 60, now) == "5m ago"
    assert fmt_ago(now - 3 * 3600, now) == "3h ago"
    assert fmt_ago(now - 2 * 86400, now) == "2d ago"
    assert fmt_ago(None, now) == "—"


def test_fmt_percent():
    assert fmt_percent(99.8) == "99.8%"
    assert fmt_percent(100) == "100%"
    assert fmt_percent(99.876) == "99.88%"
    assert fmt_percent(50.10) == "50.1%"
    assert fmt_percent(None) == "—"


def test_iso_utc():
    assert iso_utc(TS) == "2026-10-31T14:58:05Z"
    assert iso_utc(None) == "—"


def test_weekday_values_stay_stable():
    assert [value for value, _label in WEEKDAY_CHOICES] == ["5", "6", "0", "1", "2", "3", "4"]
    assert WEEKDAY_CHOICES[0][1] == "Saturday"
    assert WEEKDAY_CHOICES[-1][1] == "Friday"


def test_use_jalali_is_removed(database):
    assert "use_jalali" not in {item.key for item in DEFINITIONS}
    with pytest.raises(KeyError):
        settings.get("use_jalali")
    Setting.create(key="use_jalali", value="1")
    init_db(database.database)
    assert Setting.select().where(Setting.key == "use_jalali").count() == 0
    init_db(database.database)
    assert Setting.select().where(Setting.key == "use_jalali").count() == 0
