"""Public status page copy. English and Persian. Nothing else in the app is translated.

A missing Persian key falls back to English. ``t`` is the only lookup.
"""

from __future__ import annotations

EN: dict[str, str] = {
    "theme": "Toggle color theme",
    "language": "Language",
    "lang_en": "EN",
    "lang_fa": "فا",
    "sort": "Sort",
    "tab_all": "All",
    "tab_fastest": "Fastest",
    "tab_stable": "Most stable",
    "search_placeholder": "Search servers…",
    "search_label": "Search servers",
    "no_match": "No matching servers.",
    "footer_refresh": "Auto-refreshes every 60s",
    "footer_times": "Times are shown in",
    "empty": "No servers to show yet.",
    "status_online": "Online",
    "status_degraded": "Degraded",
    "status_down": "Down",
    "status_new": "New",
    "status_unknown": "Unknown",
    "collecting": "Collecting data",
    "for": "for",
    "uptime": "uptime",
    "no_checks": "No recent checks",
    "stat_uptime": "Uptime",
    "stat_ping": "Avg ping",
    "stat_incidents": "Incidents",
    "range_24h": "24h",
    "range_7d": "7d",
    "range_30d": "30d",
    "range_group": "Range",
    "range_long_24h": "24 hours",
    "range_long_7d": "7 days",
    "range_long_30d": "30 days",
    "aria_bars": "Status for the last 24 hours",
    "aria_bars_range": "Status for the selected range",
    "aria_chart": "Ping chart",
    "down_since": "Down since",
    "last_incident": "Last incident:",
    "lasted": "lasted",
    "no_incidents": "No incidents in the last 30 days",
    "last_check": "Last check:",
    "test_location": "Test location:",
    "error_404": "Page not found.",
    "error_back": "Back to the status page",
    "error_title": "Error",
    "dur_d": "{n}d",
    "dur_h": "{n}h",
    "dur_m": "{n}m",
    "dur_s": "{n}s",
    "dur_join": " ",
    "ago_just": "just now",
    "ago_m": "{n}m ago",
    "ago_h": "{n}h ago",
    "ago_d": "{n}d ago",
    "axis_24h": "24h ago",
    "axis_now": "now",
    "axis_today": "today",
    "tip_nodata": "no data",
    "tip_uptime": "{value} uptime",
    "tip_noping": "no ping",
    "aria_status": "Status for the last {range}",
    "aria_ping": "Ping over the last {range}, average {ms} ms",
    "aria_ping_empty": "Ping over the last {range}, no data",
    "sum_all_one": "All 1 server is online",
    "sum_all": "All {n} servers are online",
    "sum_down_only": "1 of 1 server is down",
    "sum_down_one": "{n} of {total} servers is down",
    "sum_down": "{n} of {total} servers are down",
    "sum_extra_degraded": ", {n} degraded",
    "sum_deg_only": "1 of 1 server is degraded",
    "sum_deg_one": "{n} of {total} servers is degraded",
    "sum_deg": "{n} of {total} servers are degraded",
    "sum_on_one": "{n} of {total} servers is online",
    "sum_on": "{n} of {total} servers are online",
    "sum_on_none": "{n} of {total} server are online",
}

# The switch label is the only Persian word a visitor sees on the English page.
FA: dict[str, str] = {
    "theme": "تغییر رنگ",
    "language": "زبان",
    "lang_en": "EN",
    "lang_fa": "فا",
    "sort": "مرتب‌سازی",
    "tab_all": "همه",
    "tab_fastest": "سریع‌ترین",
    "tab_stable": "پایدارترین",
    "search_placeholder": "جستجوی سرور…",
    "search_label": "جستجوی سرور",
    "no_match": "سروری پیدا نشد.",
    "footer_refresh": "هر 60 ثانیه تازه می‌شود",
    "footer_times": "زمان‌ها با",
    "empty": "هنوز سروری نیست.",
    "status_online": "آنلاین",
    "status_degraded": "ناپایدار",
    "status_down": "قطع",
    "status_new": "جدید",
    "status_unknown": "نامشخص",
    "collecting": "در حال جمع‌آوری",
    "for": "برای",
    "uptime": "آپتایم",
    "no_checks": "بررسی تازه‌ای نیست",
    "stat_uptime": "آپتایم",
    "stat_ping": "میانگین پینگ",
    "stat_incidents": "قطعی‌ها",
    "range_24h": "24 ساعت",
    "range_7d": "7 روز",
    "range_30d": "30 روز",
    "range_group": "بازه",
    "range_long_24h": "24 ساعت",
    "range_long_7d": "7 روز",
    "range_long_30d": "30 روز",
    "aria_bars": "وضعیت 24 ساعت اخیر",
    "aria_bars_range": "وضعیت بازه انتخاب‌شده",
    "aria_chart": "نمودار پینگ",
    "down_since": "قطع از",
    "last_incident": "آخرین قطعی:",
    "lasted": "مدت",
    "no_incidents": "قطعی در 30 روز اخیر نیست",
    "last_check": "آخرین بررسی:",
    "test_location": "محل تست:",
    "error_404": "صفحه پیدا نشد.",
    "error_back": "بازگشت به صفحه وضعیت",
    "error_title": "خطا",
    "dur_d": "{n} روز",
    "dur_h": "{n} ساعت",
    "dur_m": "{n} دقیقه",
    "dur_s": "{n} ثانیه",
    "dur_join": " و ",
    "ago_just": "همین الان",
    "ago_m": "{n} دقیقه پیش",
    "ago_h": "{n} ساعت پیش",
    "ago_d": "{n} روز پیش",
    "axis_24h": "24 ساعت پیش",
    "axis_now": "اکنون",
    "axis_today": "امروز",
    "tip_nodata": "بدون داده",
    "tip_uptime": "{value} آپتایم",
    "tip_noping": "بدون پینگ",
    "aria_status": "وضعیت {range} اخیر",
    "aria_ping": "پینگ {range} اخیر، میانگین {ms} ms",
    "aria_ping_empty": "پینگ {range} اخیر، بدون داده",
    "sum_all_one": "تنها سرور آنلاین است",
    "sum_all": "همه {n} سرور آنلاین‌اند",
    "sum_down_only": "تنها سرور قطع است",
    "sum_down_one": "{n} سرور از {total} قطع است",
    "sum_down": "{n} سرور از {total} قطع‌اند",
    "sum_extra_degraded": "، {n} ناپایدار",
    "sum_deg_only": "تنها سرور ناپایدار است",
    "sum_deg_one": "{n} سرور از {total} ناپایدار است",
    "sum_deg": "{n} سرور از {total} ناپایدارند",
    "sum_on_one": "{n} سرور از {total} آنلاین است",
    "sum_on": "{n} سرور از {total} آنلاین‌اند",
    "sum_on_none": "{n} سرور از {total} آنلاین‌اند",
}

JS_KEYS = (
    "dur_d", "dur_h", "dur_m", "dur_s", "dur_join",
    "ago_just", "ago_m", "ago_h", "ago_d",
    "axis_24h", "axis_now", "axis_today",
    "range_long_24h", "range_long_7d", "range_long_30d",
    "tip_nodata", "tip_uptime", "tip_noping",
    "aria_status", "aria_ping", "aria_ping_empty", "aria_chart", "aria_bars_range",
)

_STATUS = {
    "online": "status_online",
    "degraded": "status_degraded",
    "offline": "status_down",
    "unknown": "status_unknown",
}


def normalize_lang(value: str | None) -> str:
    return value if value in ("en", "fa") else "en"


def t(key: str, lang: str | None = None, **kwargs: object) -> str:
    """Text for ``key``. Persian falls back to English. Unknown keys return the key."""
    chosen = normalize_lang(lang)
    text = FA.get(key) if chosen == "fa" else None
    if text is None:
        text = EN.get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def client_catalog(lang: str | None) -> dict[str, str]:
    """The strings the public page scripts need, already resolved for ``lang``."""
    chosen = normalize_lang(lang)
    return {key: t(key, chosen) for key in JS_KEYS}


def status_text(status: str, collecting: bool, lang: str | None) -> str:
    if collecting:
        return t("status_new", lang)
    return t(_STATUS.get(status, "status_unknown"), lang)


def summary_sentence(total: int, online: int, down: int, degraded: int, lang: str | None = None) -> str:
    """Screen-reader summary. English matches the previous sentences exactly."""
    if not total:
        return ""
    chosen = normalize_lang(lang)
    if online == total:
        return t("sum_all_one", chosen) if total == 1 else t("sum_all", chosen, n=total)
    if down:
        if total == 1:
            text = t("sum_down_only", chosen)
        elif down == 1:
            text = t("sum_down_one", chosen, n=down, total=total)
        else:
            text = t("sum_down", chosen, n=down, total=total)
        if degraded:
            text += t("sum_extra_degraded", chosen, n=degraded)
        return text
    if degraded:
        if total == 1:
            return t("sum_deg_only", chosen)
        if degraded == 1:
            return t("sum_deg_one", chosen, n=degraded, total=total)
        return t("sum_deg", chosen, n=degraded, total=total)
    if total == 1:
        return t("sum_on_none", chosen, n=online, total=total)
    if online == 1:
        return t("sum_on_one", chosen, n=online, total=total)
    return t("sum_on", chosen, n=online, total=total)
