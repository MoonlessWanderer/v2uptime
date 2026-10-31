"""Runtime settings stored in the ``Setting`` table.

Every setting is declared once in :data:`DEFINITIONS` with its type, default,
limits and Persian label; the admin Settings page is generated from it.
Values are cached in memory (single process) and validated on write.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.timeutil import is_valid_timezone

TELEGRAM_EVENTS: tuple[tuple[str, str], ...] = (
    ("down", "🔴 قطع شدن سرور"),
    ("up", "🟢 وصل شدن دوباره"),
    ("flapping", "⚠️ نوسان (Flapping)"),
    ("network", "🚨 قطعی شبکه سرور مانیتور"),
    ("system", "⚙️ رویدادهای سیستمی (Xray، ری‌استارت پنل)"),
    ("daily", "📊 گزارش روزانه"),
    ("weekly", "📈 گزارش هفتگی"),
)

WEEKDAY_CHOICES: tuple[tuple[str, str], ...] = (
    ("5", "Saturday"), ("6", "Sunday"), ("0", "Monday"), ("1", "Tuesday"),
    ("2", "Wednesday"), ("3", "Thursday"), ("4", "Friday"),
)

RESERVED_PREFIXES = ("/api", "/static", "/internal", "/partials", "/healthz", "/__")


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class SettingDef:
    key: str
    default: Any
    kind: str  # int float str text bool url choice multi secret time path tz chat_ids
    label: str
    group: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    internal: bool = False  # never shown in the UI


GROUPS: dict[str, str] = {
    "general": "عمومی",
    "checks": "تست اتصال",
    "status": "وضعیت‌ها و قطعی",
    "retention": "نگهداری داده",
    "telegram": "ربات تلگرام",
}

DEFINITIONS: tuple[SettingDef, ...] = (
    # --- general
    SettingDef("site_title", "وضعیت سرورها", "str", "عنوان سایت", "general"),
    SettingDef("site_description", "پایش لحظه‌ای کیفیت اتصال سرورها", "text", "توضیح سایت", "general"),
    SettingDef("test_location", "تهران - مخابرات", "str", "محل انجام تست", "general",
               "در صفحه عمومی نمایش داده می‌شود."),
    SettingDef("timezone", "Asia/Tehran", "tz", "منطقه زمانی نمایش", "general"),
    SettingDef("admin_path", "/admin", "path", "مسیر پنل ادمین", "general",
               "مثلاً /admin-x7k2 — بعد از ذخیره، پنل فقط از مسیر جدید باز می‌شود."),
    # --- checks
    SettingDef("check_interval", 60, "int", "فاصله چک‌ها (ثانیه)", "checks", min=15, max=3600),
    SettingDef("check_timeout", 10, "int", "تایم‌اوت هر تست (ثانیه)", "checks", min=2, max=60),
    SettingDef("test_url", "https://www.gstatic.com/generate_204", "url", "آدرس تست", "checks",
               "یک URL سبک؛ پاسخ 2xx موفق است. جایگزین: https://cp.cloudflare.com"),
    SettingDef("reference_url", "https://www.aparat.com", "url", "آدرس چک مرجع داخلی", "checks",
               "بدون پروکسی تست می‌شود تا قطعی اینترنت خود سرور مانیتور تشخیص داده شود."),
    SettingDef("concurrency", 20, "int", "حداکثر تست هم‌زمان", "checks", min=1, max=200),
    # --- status
    SettingDef("fail_threshold", 3, "int", "N: تعداد شکست پشت‌سرهم برای قطعی", "status", min=1, max=20),
    SettingDef("recover_threshold", 2, "int", "M: تعداد موفقیت پشت‌سرهم برای وصل شدن", "status", min=1, max=20),
    SettingDef("degraded_latency_ms", 1500, "int", "آستانه latency برای وضعیت ناپایدار (ms)", "status",
               min=100, max=60000),
    SettingDef("degraded_fail_percent", 20, "int", "درصد شکست ۱۰ دقیقه اخیر برای وضعیت ناپایدار", "status",
               min=1, max=100),
    SettingDef("flap_window_minutes", 30, "int", "بازه تشخیص نوسان (دقیقه)", "status", min=5, max=240),
    SettingDef("flap_threshold", 4, "int", "حداکثر قطع/وصل در بازه نوسان", "status", min=2, max=50),
    # --- retention
    SettingDef("raw_retention_hours", 48, "int", "نگهداری دیتای خام (ساعت)", "retention", min=26, max=24 * 31),
    SettingDef("hourly_retention_days", 90, "int", "نگهداری دیتای ساعتی (روز)", "retention", min=31, max=730),
    # --- telegram
    SettingDef("telegram_enabled", False, "bool", "فعال بودن ربات", "telegram"),
    SettingDef("telegram_token", "", "secret", "Bot Token", "telegram"),
    SettingDef("telegram_chat_ids", "", "chat_ids", "Chat ID ها", "telegram",
               "آیدی عددی؛ چند مورد را با کاما جدا کنید (گروه/کانال: -100...)."),
    SettingDef("telegram_send_mode", "auto", "choice", "روش ارسال", "telegram",
               choices=(("auto", "خودکار (اول مستقیم، بعد از طریق سرورها)"),
                        ("direct", "فقط مستقیم"), ("via_server", "فقط از طریق سرورها"))),
    SettingDef("telegram_preferred_server", 0, "int", "سرور ترجیحی برای ارسال", "telegram",
               "۰ یعنی انتخاب خودکار.", min=0),
    SettingDef("telegram_events", ",".join(k for k, _ in TELEGRAM_EVENTS), "multi", "رویدادهای اعلان", "telegram",
               choices=TELEGRAM_EVENTS),
    SettingDef("telegram_daily_time", "09:00", "time", "ساعت گزارش روزانه", "telegram"),
    SettingDef("telegram_weekly_day", "5", "choice", "روز گزارش هفتگی", "telegram", choices=WEEKDAY_CHOICES),
    SettingDef("telegram_commands", True, "bool", "پاسخ به دستورهای ربات (/status ...)", "telegram"),
    # --- internal state
    SettingDef("agg_until", 0, "int", "", "internal", internal=True),
    SettingDef("telegram_update_offset", 0, "int", "", "internal", internal=True),
    SettingDef("last_daily_report", "", "str", "", "internal", internal=True),
    SettingDef("last_weekly_report", "", "str", "", "internal", internal=True),
)

BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFINITIONS}

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_CHAT_RE = re.compile(r"^-?\d{1,20}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-/]{1,63}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def validate(key: str, raw: Any) -> Any:
    """Validate and normalise a raw (form) value; return the typed value."""
    d = BY_KEY.get(key)
    if d is None:
        raise SettingError(f"تنظیم ناشناخته: {key}")
    text = raw.strip() if isinstance(raw, str) else raw

    if d.kind == "bool":
        return _to_bool(text)
    if d.kind in ("int", "float"):
        try:
            value: float = int(text) if d.kind == "int" else float(text)
        except (TypeError, ValueError):
            raise SettingError(f"«{d.label}» باید عدد باشد.") from None
        if d.min is not None and value < d.min:
            raise SettingError(f"«{d.label}» نباید کمتر از {d.min:g} باشد.")
        if d.max is not None and value > d.max:
            raise SettingError(f"«{d.label}» نباید بیشتر از {d.max:g} باشد.")
        return value
    text = "" if text is None else str(text)
    if d.kind == "url":
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SettingError(f"«{d.label}» باید یک آدرس http یا https معتبر باشد.")
        return text
    if d.kind == "tz":
        if not is_valid_timezone(text):
            raise SettingError("منطقه زمانی نامعتبر است (مثال: Asia/Tehran).")
        return text
    if d.kind == "path":
        text = "/" + text.strip("/")
        if not _PATH_RE.match(text) or "//" in text:
            raise SettingError("مسیر پنل فقط می‌تواند شامل حروف انگلیسی، عدد، - و _ باشد.")
        if any(text.startswith(p) for p in RESERVED_PREFIXES):
            raise SettingError("این مسیر رزرو شده است؛ مسیر دیگری انتخاب کنید.")
        return text
    if d.kind == "time":
        if not _TIME_RE.match(text):
            raise SettingError("ساعت باید به شکل HH:MM باشد (مثلاً 09:00).")
        return text
    if d.kind == "choice":
        if text not in {c for c, _ in d.choices}:
            raise SettingError(f"مقدار «{d.label}» نامعتبر است.")
        return text
    if d.kind == "multi":
        allowed = {c for c, _ in d.choices}
        items = [i.strip() for i in text.split(",") if i.strip()]
        bad = [i for i in items if i not in allowed]
        if bad:
            raise SettingError(f"مقدار نامعتبر برای «{d.label}»: {', '.join(bad)}")
        return ",".join(items)
    if d.kind == "chat_ids":
        items = [i.strip() for i in re.split(r"[,\s]+", text) if i.strip()]
        bad = [i for i in items if not _CHAT_RE.match(i)]
        if bad:
            raise SettingError(f"Chat ID باید عددی باشد: {', '.join(bad)}")
        return ",".join(items)
    if d.kind == "secret":
        if key == "telegram_token" and text and not _TOKEN_RE.match(text):
            raise SettingError("فرمت Bot Token نامعتبر است (مثال: 123456:ABC-DEF...).")
        return text
    if d.kind == "str" and len(text) > 200:
        raise SettingError(f"«{d.label}» بیش از حد طولانی است.")
    if d.kind == "text" and len(text) > 2000:
        raise SettingError(f"«{d.label}» بیش از حد طولانی است.")
    return text


def _decode(d: SettingDef, stored: str) -> Any:
    try:
        if d.kind == "bool":
            return _to_bool(stored)
        if d.kind == "int":
            return int(stored)
        if d.kind == "float":
            return float(stored)
    except ValueError:
        return d.default
    return stored


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "•" * 8 + value[-4:] if len(value) > 10 else "•" * 8


class SettingsStore:
    """Thread-safe cached access to settings."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> None:
        from app.models import Setting

        rows = {s.key: s.value for s in Setting.select()}
        with self._lock:
            self._cache = rows
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, key: str) -> Any:
        d = BY_KEY[key]
        self._ensure_loaded()
        with self._lock:
            stored = self._cache.get(key)
        return d.default if stored is None else _decode(d, stored)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def all(self) -> dict[str, Any]:
        return {d.key: self.get(d.key) for d in DEFINITIONS}

    def set(self, key: str, value: Any, validated: bool = False) -> None:
        self.set_many({key: value}, validated=validated)

    def set_many(self, values: dict[str, Any], validated: bool = False) -> None:
        """Validate (unless already validated) and persist values atomically."""
        from app.db import db
        from app.models import Setting

        typed = values if validated else {k: validate(k, v) for k, v in values.items()}
        encoded = {k: _encode(v) for k, v in typed.items()}
        with db.atomic():
            for key, value in encoded.items():
                Setting.insert(key=key, value=value).on_conflict_replace().execute()
        with self._lock:
            self._cache.update(encoded)

    # Convenience accessors -------------------------------------------------
    @property
    def tz(self) -> str:
        return self.get("timezone")

    def chat_ids(self) -> list[str]:
        return [c for c in self.get("telegram_chat_ids").split(",") if c]

    def telegram_events(self) -> set[str]:
        return {e for e in self.get("telegram_events").split(",") if e}


settings = SettingsStore()
