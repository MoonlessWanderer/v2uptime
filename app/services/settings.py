"""Runtime settings stored in the ``Setting`` table.

Every setting is declared once in :data:`DEFINITIONS` with its type, default,
limits and label; the admin Settings page is generated from it.
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
    ("down", "Server down"),
    ("up", "Server back online"),
    ("flapping", "Flapping"),
    ("network", "Monitor network outage"),
    ("system", "System events (Xray, panel restart)"),
    ("daily", "Daily report"),
    ("weekly", "Weekly report"),
    ("security", "Security alerts"),
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
    kind: str  # int float str text bool url choice multi secret time path tz chat_ids dns
    label: str
    group: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    internal: bool = False  # never shown in the UI


GROUPS: dict[str, str] = {
    "general": "General",
    "checks": "Checks",
    "status": "Status and incidents",
    "retention": "Retention",
    "dns": "DNS for config domains",
    "telegram": "Telegram",
}

# Collapsed on the settings pages. Defaults stay as declared below.
ADVANCED_KEYS = frozenset({
    "admin_path", "concurrency", "reference_url", "fail_threshold", "recover_threshold",
    "degraded_latency_ms", "degraded_fail_percent", "flap_window_minutes", "flap_threshold",
    "raw_retention_hours", "hourly_retention_days", "telegram_preferred_server",
})

DEFINITIONS: tuple[SettingDef, ...] = (
    # --- general
    SettingDef("site_title", "Server Status", "str", "Site title", "general",
               "Shown in the header and the browser title."),
    SettingDef("site_description", "Real-time connection quality of our servers", "text", "Site description", "general",
               "One line under the title. Leave empty to hide it."),
    SettingDef("test_location", "", "str", "Test location", "general",
               "Shown in Telegram reports."),
    SettingDef("public_language", "en", "choice", "Public page language", "general",
               "Language of the status page.",
               choices=(("en", "English"), ("fa", "Persian"))),
    SettingDef("site_title_fa", "", "str", "Site title (Persian)", "general",
               "Used when the status page is in Persian. Leave empty to use the main title."),
    SettingDef("site_description_fa", "", "text", "Site description (Persian)", "general",
               "Used when the status page is in Persian. Leave empty to use the main description."),
    SettingDef("timezone", "Asia/Tehran", "tz", "Display timezone", "general",
               "Times are stored in UTC and shown in this timezone."),
    SettingDef("admin_path", "/admin", "path", "Admin path", "general",
               "For example /admin-x7k2. After saving, the panel opens only at the new path."),
    # --- checks
    SettingDef("check_interval", 60, "int", "Check interval (seconds)", "checks",
               "How often every server is tested.", min=15, max=3600),
    SettingDef("check_timeout", 10, "int", "Timeout per check (seconds)", "checks",
               "A check that takes longer counts as a timeout.", min=2, max=60),
    SettingDef("test_url", "https://www.gstatic.com/generate_204", "url", "Test URL", "checks",
               "A small URL. A 2xx response is a success. Alternative: https://cp.cloudflare.com"),
    SettingDef("reference_url", "https://www.aparat.com", "url", "Reference URL", "checks",
               "Fetched without a proxy to detect when the monitor's own internet is down."),
    SettingDef("concurrency", 20, "int", "Maximum parallel checks", "checks",
               "How many servers are tested at the same time.", min=1, max=200),
    # --- status
    SettingDef("fail_threshold", 3, "int", "N: failures in a row before an incident", "status",
               "An incident opens after this many failed checks in a row.", min=1, max=20),
    SettingDef("recover_threshold", 2, "int", "M: successes in a row to close an incident", "status",
               "An incident closes after this many successful checks in a row.", min=1, max=20),
    SettingDef("degraded_latency_ms", 1500, "int", "Latency threshold for degraded (ms)", "status",
               "A successful check slower than this is Degraded.", min=100, max=60000),
    SettingDef("degraded_fail_percent", 20, "int", "Recent failure percent for degraded", "status",
               "Degraded when more than this percent of the last 10 minutes failed.", min=1, max=100),
    SettingDef("flap_window_minutes", 30, "int", "Flap window (minutes)", "status",
               "Window used to detect a server that keeps changing status.", min=5, max=240),
    SettingDef("flap_threshold", 4, "int", "Maximum status changes in the flap window", "status",
               "More changes than this pauses individual down and up alerts.", min=2, max=50),
    # --- retention
    SettingDef("raw_retention_hours", 48, "int", "Raw check retention (hours)", "retention",
               "Individual check rows older than this are deleted. Minimum 26.", min=26, max=24 * 31),
    SettingDef("hourly_retention_days", 90, "int", "Hourly data retention (days)", "retention",
               "Hourly rollups and closed incidents older than this are deleted.", min=31, max=730),
    # --- dns (config domains; empty keeps Xray's current AsIs behaviour)
    SettingDef("dns_primary", "", "dns", "Primary DNS", "dns",
               "Tried first. An IP (8.8.8.8), IP:port (8.8.8.8:53), tcp://IP, "
               "https://IP/dns-query (use an IP, not a domain name), or localhost. "
               "Leave both empty to keep the current behaviour."),
    SettingDef("dns_secondary", "", "dns", "Secondary DNS", "dns",
               'Tip: use "localhost" as the secondary to fall back to the server\'s own DNS.'),
    SettingDef("dns_ipv4_only", False, "bool", "IPv4 only", "dns",
               "Only request A records, for a server with no working IPv6."),
    # --- telegram
    SettingDef("telegram_enabled", False, "bool", "Enable the bot", "telegram",
               "When off, alerts stay queued and nothing is sent."),
    SettingDef("telegram_token", "", "secret", "Bot token", "telegram",
               "Shown masked. Enter a new value to replace it."),
    SettingDef("telegram_chat_ids", "", "chat_ids", "Chat IDs", "telegram",
               "Numeric IDs, separated by commas. Groups and channels use -100…."),
    SettingDef("telegram_send_mode", "auto", "choice", "Send mode", "telegram",
               "Automatic tries a direct send, then up to four online servers.",
               choices=(("auto", "Automatic (direct, then via servers)"),
                        ("direct", "Direct only"), ("via_server", "Via servers only"))),
    SettingDef("telegram_preferred_server", 0, "int", "Preferred server for sending", "telegram",
               "Used first when sending through a server. Automatic picks one.", min=0),
    SettingDef("telegram_events", ",".join(k for k, _ in TELEGRAM_EVENTS), "multi", "Notification events", "telegram",
               "Which events are queued for Telegram.", choices=TELEGRAM_EVENTS),
    SettingDef("telegram_daily_time", "09:00", "time", "Daily report time", "telegram",
               "Local time in the display timezone. The first day is skipped."),
    SettingDef("telegram_weekly_day", "5", "choice", "Weekly report day", "telegram",
               "Sent at the daily report time on this weekday.", choices=WEEKDAY_CHOICES),
    SettingDef("telegram_commands", True, "bool", "Answer bot commands (/status, …)", "telegram",
               "Only registered chat IDs receive a reply."),
    # --- internal state
    SettingDef("agg_until", 0, "int", "", "internal", internal=True),
    SettingDef("telegram_update_offset", 0, "int", "", "internal", internal=True),
    SettingDef("last_daily_report", "", "str", "", "internal", internal=True),
    SettingDef("last_weekly_report", "", "str", "", "internal", internal=True),
    SettingDef("security_event_added", False, "bool", "", "internal", internal=True),
    SettingDef("network_down_since", 0, "int", "", "internal", internal=True),
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
        raise SettingError(f"Unknown setting: {key}")
    text = raw.strip() if isinstance(raw, str) else raw

    if d.kind == "bool":
        return _to_bool(text)
    if d.kind in ("int", "float"):
        try:
            value: float = int(text) if d.kind == "int" else float(text)
        except (TypeError, ValueError):
            raise SettingError(f"{d.label} must be a number.") from None
        if d.min is not None and value < d.min:
            raise SettingError(f"Must be at least {d.min:g}.")
        if d.max is not None and value > d.max:
            raise SettingError(f"Must be at most {d.max:g}.")
        return value
    text = "" if text is None else str(text)
    if d.kind == "url":
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SettingError(f"{d.label} must be a valid http or https URL.")
        return text
    if d.kind == "tz":
        if not is_valid_timezone(text):
            raise SettingError("Invalid timezone (example: Asia/Tehran).")
        return text
    if d.kind == "path":
        text = "/" + text.strip("/")
        if not _PATH_RE.match(text) or "//" in text:
            raise SettingError("Admin path may only contain letters, digits, hyphens, and underscores.")
        if any(text.startswith(p) for p in RESERVED_PREFIXES):
            raise SettingError("This path is reserved. Choose another.")
        return text
    if d.kind == "time":
        if not _TIME_RE.match(text):
            raise SettingError("Time must be HH:MM (for example 09:00).")
        return text
    if d.kind == "choice":
        if text not in {c for c, _ in d.choices}:
            raise SettingError(f"Invalid value for {d.label}.")
        return text
    if d.kind == "multi":
        allowed = {c for c, _ in d.choices}
        items = [i.strip() for i in text.split(",") if i.strip()]
        bad = [i for i in items if i not in allowed]
        if bad:
            raise SettingError(f"Invalid value for {d.label}: {', '.join(bad)}")
        return ",".join(items)
    if d.kind == "chat_ids":
        items = [i.strip() for i in re.split(r"[,\s]+", text) if i.strip()]
        bad = [i for i in items if not _CHAT_RE.match(i)]
        if bad:
            raise SettingError(f"Chat ID must be numeric: {', '.join(bad)}")
        return ",".join(items)
    if d.kind == "secret":
        if key == "telegram_token" and text and not _TOKEN_RE.match(text):
            raise SettingError("Invalid bot token format (example: 123456:ABC-DEF...).")
        return text
    if d.kind == "dns":
        from app.services.dns import parse_dns
        return parse_dns(text)
    if d.kind == "str" and len(text) > 200:
        raise SettingError(f"{d.label} is too long.")
    if d.kind == "text" and len(text) > 2000:
        raise SettingError(f"{d.label} is too long.")
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
