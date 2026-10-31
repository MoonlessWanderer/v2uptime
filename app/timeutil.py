"""Time helpers: UTC epoch storage, local/Jalali display, Persian digits.

Every timestamp in the database is an integer Unix epoch (seconds, UTC).
Conversion to the display timezone happens only when rendering.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
GREGORIAN_MONTHS = (
    "ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن",
    "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر",
)
# Python weekday() order: Monday=0 ... Sunday=6
WEEKDAYS_FA = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")

HOUR = 3600
DAY = 86400


def now_ts() -> int:
    return int(time.time())


def fa(value: object) -> str:
    """Convert ASCII digits to Persian digits."""
    return str(value).translate(FA_DIGITS)


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


def fmt_datetime(ts: int | None, tz_name: str, jalali: bool = True, seconds: bool = False) -> str:
    if ts is None:
        return "—"
    local = to_local(ts, tz_name)
    clock = local.strftime("%H:%M:%S" if seconds else "%H:%M")
    if jalali:
        j = jdatetime.date.fromgregorian(date=local.date())
        return fa(f"{j.year:04d}/{j.month:02d}/{j.day:02d} {clock}")
    return fa(f"{local:%Y-%m-%d} {clock}")


def fmt_date_long(ts: int, tz_name: str, jalali: bool = True) -> str:
    local = to_local(ts, tz_name)
    if jalali:
        j = jdatetime.date.fromgregorian(date=local.date())
        return fa(f"{j.day} {JALALI_MONTHS[j.month - 1]} {j.year}")
    return fa(f"{local.day} {GREGORIAN_MONTHS[local.month - 1]} {local.year}")


def fmt_time(ts: int, tz_name: str) -> str:
    return fa(to_local(ts, tz_name).strftime("%H:%M"))


def fmt_range(start: int, end: int, tz_name: str, jalali: bool = True, with_date: bool = True) -> str:
    """Human label for a bucket, e.g. '۹ آبان ۱۴۰۵ ۱۲:۰۰ تا ۱۲:۳۰'."""
    if not with_date:
        return f"{fmt_date_long(start, tz_name, jalali)}"
    end_label = fmt_time(end, tz_name)
    return f"{fmt_date_long(start, tz_name, jalali)} {fmt_time(start, tz_name)} تا {end_label}"


def fmt_duration(seconds: int | float | None) -> str:
    """'۲ ساعت و ۵ دقیقه' style durations."""
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return fa(f"{seconds} ثانیه")
    days, rem = divmod(seconds, DAY)
    hours, rem = divmod(rem, HOUR)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes and not days:
        parts.append(f"{minutes} دقیقه")
    return fa(" و ".join(parts) or "۰ دقیقه")


def fmt_ago(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return "—"
    delta = (now or now_ts()) - ts
    if delta < 60:
        return "لحظاتی پیش"
    return f"{fmt_duration(delta)} پیش"


def fmt_percent(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"
    return fa(f"{text}٪")


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def safe_float(value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return value
