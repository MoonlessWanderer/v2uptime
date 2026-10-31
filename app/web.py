"""Jinja2 environment, template filters and small presentation helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from app import __version__
from app.services.checker import error_label
from app.services.settings import settings
from app.public_i18n import status_text, summary_sentence, t
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


def _page_lang(context: dict) -> str:
    lang = context.get("lang")
    return lang if lang in ("en", "fa") else "en"


@pass_context
def time_tag(context: dict, value: Any, kind: str = "time") -> Markup | str:
    """`<time datetime="…Z" title="YYYY-MM-DD HH:mm:ss UTC+00:00">…</time>`."""
    ts = _coerce_ts(value)
    if ts is None:
        return "—"
    zone = settings.tz
    lang = _page_lang(context)
    title = f"{fmt_datetime(ts, zone, seconds=True)} {tz_label(zone, ts)}"
    if kind == "datetime":
        visible = fmt_datetime(ts, zone)
    elif kind == "seconds":
        visible = fmt_datetime(ts, zone, seconds=True)
    elif kind == "ago":
        visible = fmt_ago(ts, lang=lang)
    else:
        visible = fmt_time(ts, zone)
    ago = " data-ago" if kind == "ago" else ""
    return Markup(
        f'<time datetime="{escape(iso_utc(ts))}" title="{escape(title)}"{ago}>{escape(visible)}</time>'
    )


@pass_context
def _duration(context: dict, value: Any, coarse: bool = False) -> str:
    return fmt_duration(value, coarse, _page_lang(context))


@pass_context
def _t(context: dict, key: str, **kwargs: object) -> str:
    return t(key, _page_lang(context), **kwargs)


@pass_context
def _status_text(context: dict, status: str, collecting: bool = False) -> str:
    return status_text(status, collecting, _page_lang(context))


@pass_context
def _summary(context: dict, total: int, online: int, down: int, degraded: int) -> str:
    return summary_sentence(total, online, down, degraded, _page_lang(context))


templates.env.globals.update(
    admin_url=admin_url, static_url=static_url, settings=settings, version=__version__,
    tz_label=tz_label, t=_t, status_text=_status_text, summary_sentence=_summary,
)
templates.env.filters.update(
    time_tag=time_tag, bidi=bidi, duration=_duration, percent=fmt_percent, ago=fmt_ago, ms=fmt_ms,
    error_label=error_label, status_label=lambda s: STATUS_LABELS.get(s, s),
)


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
