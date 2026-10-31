"""Time helpers: UTC epoch storage, Gregorian display, Latin digits.

Every timestamp in the database is an integer Unix epoch (seconds, UTC).
Conversion to the display timezone happens only when rendering. Hours are
24-hour; empty values render as an em dash.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HOUR = 3600
DAY = 86400
_EMPTY = "—"


def now_ts() -> int:
    return int(time.time())


def hour_floor(ts: int) -> int:
    return ts - ts % HOUR


@lru_cache(maxsize=32)
def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def to_local(ts: int, tz_name: str) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(get_tz(tz_name))


def local_midnight(ts: int, tz_name: str) -> int:
    """Epoch of 00:00 local time on the day containing ``ts``."""
    local = to_local(ts, tz_name)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def add_local_days(ts: int, days: int, tz_name: str) -> int:
    """Shift a local-midnight epoch by whole days (DST safe)."""
    local = to_local(ts, tz_name)
    shifted = (local.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=get_tz(tz_name))
    return int(shifted.timestamp())


def utc_offset(ts: int, tz_name: str) -> int:
    offset = to_local(ts, tz_name).utcoffset()
    return int(offset.total_seconds()) if offset else 0


def tz_offset(tz_name: str, ts: int | None = None) -> str:
    """Offset as ``+03:30`` or ``-05:00`` at ``ts`` (or now)."""
    seconds = utc_offset(now_ts() if ts is None else ts, tz_name)
    sign = "+" if seconds >= 0 else "-"
    minutes, _sec = divmod(abs(seconds), 60)
    hours, mins = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def tz_label(tz_name: str, ts: int | None = None) -> str:
    """Offset label such as ``UTC+03:30``."""
    return "UTC" + tz_offset(tz_name, ts)


def iso_utc(ts: int | None) -> str:
    """RFC 3339 UTC timestamp with a ``Z`` suffix, or an em dash when empty."""
    if ts is None:
        return _EMPTY
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_datetime(ts: int | None, tz_name: str, seconds: bool = False) -> str:
    if ts is None:
        return _EMPTY
    local = to_local(int(ts), tz_name)
    clock = "%H:%M:%S" if seconds else "%H:%M"
    return local.strftime(f"%Y-%m-%d {clock}")


def fmt_date(ts: int | None, tz_name: str) -> str:
    if ts is None:
        return _EMPTY
    return to_local(int(ts), tz_name).strftime("%Y-%m-%d")


def fmt_time(ts: int | None, tz_name: str) -> str:
    if ts is None:
        return _EMPTY
    return to_local(int(ts), tz_name).strftime("%H:%M")


def fmt_range(start: int | None, end: int | None, tz_name: str) -> str:
    """Date plus a clock range, e.g. ``2026-10-31 12:00–12:30``."""
    if start is None or end is None:
        return _EMPTY
    start_text = fmt_datetime(start, tz_name)
    if fmt_date(start, tz_name) == fmt_date(end, tz_name):
        return f"{start_text}–{fmt_time(end, tz_name)}"
    return f"{start_text}–{fmt_datetime(end, tz_name)}"


def fmt_duration(seconds: int | float | None) -> str:
    """Two largest non-zero units: ``45s``, ``1m``, ``2h 15m``, ``3d 4h``."""
    if seconds is None:
        return _EMPTY
    total = int(max(0, seconds))
    days, rem = divmod(total, DAY)
    hours, rem = divmod(rem, HOUR)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts[:2])


def fmt_ago(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return _EMPTY
    delta = (now if now is not None else now_ts()) - int(ts)
    if delta < 60:
        return "just now"
    if delta < HOUR:
        return f"{delta // 60}m ago"
    if delta < DAY:
        return f"{delta // HOUR}h ago"
    return f"{delta // DAY}d ago"


def fmt_percent(value: float | None, digits: int = 2) -> str:
    if value is None:
        return _EMPTY
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"
    return f"{text}%"


def fmt_ms(value: float | int | None) -> str:
    if value is None:
        return _EMPTY
    return f"{int(round(value))} ms"


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def safe_float(value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return value
