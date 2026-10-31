"""Jinja2 environment, template filters and small presentation helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import __version__
from app.services.checker import error_label
from app.services.parser import flag_emoji
from app.services.settings import settings
from app.services.status import STATUS_LABELS
from app.timeutil import fmt_ago, fmt_datetime, fmt_duration, fmt_ms, fmt_percent, fmt_time, iso_utc, tz_label

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
ADMIN_INTERNAL_PREFIX = "/__admin"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def admin_url(path: str = "") -> str:
    return settings.get("admin_path") + path


def static_url(path: str) -> str:
    return f"/static/{path}?v={__version__}"


def _coerce_ts(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text == "—":
            return None
        if text.endswith("Z") and "T" in text:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def bidi(value: object) -> str:
    """Unicode isolates for a name inside an attribute (confirm dialogs, titles)."""
    return f"\u2068{'' if value is None else value}\u2069"


def time_tag(value: Any, kind: str = "time") -> Markup | str:
    """`<time datetime="…Z" title="YYYY-MM-DD HH:mm:ss UTC+00:00">…</time>`."""
    ts = _coerce_ts(value)
    if ts is None:
        return "—"
    zone = settings.tz
    title = f"{fmt_datetime(ts, zone, seconds=True)} {tz_label(zone, ts)}"
    if kind == "datetime":
        visible = fmt_datetime(ts, zone)
    elif kind == "seconds":
        visible = fmt_datetime(ts, zone, seconds=True)
    elif kind == "ago":
        visible = fmt_ago(ts)
    else:
        visible = fmt_time(ts, zone)
    ago = " data-ago" if kind == "ago" else ""
    return Markup(
        f'<time datetime="{escape(iso_utc(ts))}" title="{escape(title)}"{ago}>{escape(visible)}</time>'
    )


templates.env.globals.update(
    admin_url=admin_url, static_url=static_url, settings=settings, version=__version__,
    flag_emoji=flag_emoji, tz_label=tz_label,
)
templates.env.filters.update(
    time_tag=time_tag, bidi=bidi, duration=fmt_duration, percent=fmt_percent, ago=fmt_ago, ms=fmt_ms,
    error_label=error_label, status_label=lambda s: STATUS_LABELS.get(s, s),
)


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
