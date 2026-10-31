import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

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


DURATIONS = [
    (0, "0s"), (45, "45s"), (60, "1m"), (90, "1m 30s"), (3600, "1h"), (3601, "1h"), (3660, "1h 1m"),
    (8130, "2h 15m"), (86400, "1d"), (86401, "1d"), (90000, "1d 1h"), (273600, "3d 4h"),
]
TIME_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "time.js"
NODE_RUNNER = """
const fs = require("fs"), vm = require("vm");
const sandbox = {window: {}, setInterval() {},
                 document: {readyState: "complete", body: null, querySelectorAll: () => [], addEventListener() {}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const input = JSON.parse(process.argv[2]);
const coarse = process.argv[3] === "coarse";
process.stdout.write(JSON.stringify(input.map((s) => sandbox.window.UptimeTime.formatDuration(s, coarse))));
"""


@pytest.mark.parametrize("seconds, expected", DURATIONS)
def test_fmt_duration_table(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_fmt_duration_edges():
    assert fmt_duration(59) == "59s"
    assert fmt_duration(3599) == "59m 59s"
    assert fmt_duration(-5) == "0s"
    assert fmt_duration(None) == "—"


COARSE = [
    (45, "45s"), (59, "59s"), (60, "1m"), (90, "1m"), (47 * 60 + 2, "47m"),
    (3599, "59m"), (3600, "1h"), (3630, "1h"), (3660, "1h 1m"), (3690, "1h 1m"),
]


@pytest.mark.parametrize("seconds, expected", COARSE)
def test_fmt_duration_coarse_drops_seconds_after_a_minute(seconds, expected):
    assert fmt_duration(seconds, coarse=True) == expected
    assert fmt_duration(None, coarse=True) == "—"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def _js_durations(cases, coarse=False):
    args = ["node", "-e", NODE_RUNNER, str(TIME_JS), json.dumps([value for value, _ in cases])]
    if coarse:
        args.append("coarse")
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)
    assert json.loads(out.stdout) == [expected for _, expected in cases]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_js_format_duration_matches_python():
    _js_durations(DURATIONS + [(59, "59s"), (3599, "59m 59s"), (-5, "0s")])


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_js_format_duration_coarse_matches_python():
    _js_durations(COARSE, coarse=True)


def _utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_fmt_range_ending_at_midnight_keeps_one_date():
    # 2026-10-30 23:30 -> 2026-10-31 00:00 in Asia/Tehran (UTC+03:30)
    assert fmt_range(_utc(2026, 10, 30, 20, 0), _utc(2026, 10, 30, 20, 30), TEHRAN) == "2026-10-30 23:30–00:00"
    # 4-hour bucket of the 7d bar: 20:00 -> 00:00
    assert fmt_range(_utc(2026, 10, 30, 16, 30), _utc(2026, 10, 30, 20, 30), TEHRAN) == "2026-10-30 20:00–00:00"


def test_fmt_range_into_next_day_shows_end_date():
    start = _utc(2026, 10, 30, 20, 0)  # 23:30 local
    assert fmt_range(start, _utc(2026, 10, 30, 21, 0), TEHRAN) == "2026-10-30 23:30–2026-10-31 00:30"
    assert fmt_range(start, _utc(2026, 10, 31, 20, 30), TEHRAN) == "2026-10-30 23:30–2026-11-01 00:00"


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
