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
_DURATION_UNITS = (("d", DAY), ("h", HOUR), ("m", 60), ("s", 1))


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
    """Date plus a clock range, e.g. ``2026-10-31 12:00–12:30``.

    A range that ends exactly at the next local midnight keeps one date
    (``2026-10-30 23:30–00:00``); the end date is written only when the
    range really runs into the next day.
    """
    if start is None or end is None:
        return _EMPTY
    start_text = fmt_datetime(start, tz_name)
    next_midnight = add_local_days(local_midnight(start, tz_name), 1, tz_name)
    if fmt_date(start, tz_name) == fmt_date(end, tz_name) or end == next_midnight:
        return f"{start_text}–{fmt_time(end, tz_name)}"
    return f"{start_text}–{fmt_datetime(end, tz_name)}"


def _duration_piece(count: int, suffix: str, lang: str) -> str:
    from app.public_i18n import t

    return t(f"dur_{suffix}", lang).replace("{n}", str(count))


def fmt_duration(seconds: int | float | None, coarse: bool = False, lang: str = "en") -> str:
    """Largest non-zero unit, plus the next smaller unit when it is not zero.

    ``45s``, ``1m 30s``, ``1h`` (3601s), ``1h 1m``, ``1d`` (86401s), ``3d 4h``.
    ``coarse`` drops seconds once the span is at least a minute (``47m 2s`` → ``47m``).
    ``lang`` changes the unit words only. Keep in step with ``formatDuration`` in ``static/js/time.js``.
    """
    from app.public_i18n import normalize_lang, t

    if seconds is None:
        return _EMPTY
    chosen = normalize_lang(lang)
    rem = int(max(0, seconds))
    values = []
    for suffix, size in _DURATION_UNITS:
        count, rem = divmod(rem, size)
        values.append((count, suffix))
    for index, (count, suffix) in enumerate(values):
        if count:
            text = _duration_piece(count, suffix, chosen)
            nxt = index + 1
            if nxt < len(values) and values[nxt][0] and not (coarse and values[nxt][1] == "s"):
                text += t("dur_join", chosen) + _duration_piece(values[nxt][0], values[nxt][1], chosen)
            return text
    return _duration_piece(0, "s", chosen)


def fmt_ago(ts: int | None, now: int | None = None, lang: str = "en") -> str:
    """Relative time. ``lang`` changes the words only."""
    from app.public_i18n import normalize_lang, t

    if ts is None:
        return _EMPTY
    chosen = normalize_lang(lang)
    delta = (now if now is not None else now_ts()) - int(ts)
    if delta < 60:
        return t("ago_just", chosen)
    if delta < HOUR:
        return t("ago_m", chosen).replace("{n}", str(delta // 60))
    if delta < DAY:
        return t("ago_h", chosen).replace("{n}", str(delta // HOUR))
    return t("ago_d", chosen).replace("{n}", str(delta // DAY))


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
