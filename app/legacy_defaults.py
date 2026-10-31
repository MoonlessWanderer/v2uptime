"""Previous built-in defaults, used only to migrate untouched databases.

Admin-edited values must not be changed. This file is the only place those
old strings live, so the rest of the source can stay free of them.
"""

LEGACY_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("site_title", "وضعیت سرورها", "Server Status"),
    ("site_description", "پایش لحظه‌ای کیفیت اتصال سرورها", "Real-time connection quality of our servers"),
    ("test_location", "تهران - مخابرات", ""),
)
