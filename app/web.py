"""Jinja2 environment, template filters and small presentation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.services.checker import error_label
from app.services.parser import flag_emoji
from app.services.settings import settings
from app.services.status import STATUS_LABELS
from app.timeutil import fa, fmt_ago, fmt_datetime, fmt_duration, fmt_percent

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
ADMIN_INTERNAL_PREFIX = "/__admin"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def admin_url(path: str = "") -> str:
    return settings.get("admin_path") + path


def static_url(path: str) -> str:
    return f"/static/{path}?v={__version__}"


def sparkline(values: Iterable[int | None], width: int = 240, height: int = 36) -> dict[str, Any]:
    """Geometry for an inline SVG sparkline; ``None`` values are failures (gaps)."""
    values = list(values)
    if not values:
        return {"segments": [], "fails": [], "width": width, "height": height}
    present = [v for v in values if v is not None]
    top = max(present) if present else 1
    bottom = min(present) if present else 0
    span = max(top - bottom, 1)
    step = width / max(len(values) - 1, 1)
    pad = 3
    segments: list[str] = []
    current: list[str] = []
    fails: list[float] = []
    for index, value in enumerate(values):
        x = round(index * step, 1)
        if value is None:
            fails.append(x)
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        y = round(pad + (height - 2 * pad) * (1 - (value - bottom) / span), 1)
        current.append(f"{x},{y}")
    if current:
        segments.append(" ".join(current))
    return {"segments": segments, "fails": fails, "width": width, "height": height}


def _fmt_dt(ts: int | None, seconds: bool = False) -> str:
    return fmt_datetime(ts, settings.tz, settings.jalali, seconds=seconds)


def _fmt_ms(value: float | int | None) -> str:
    return "—" if value is None else fa(f"{int(round(value))}")


templates.env.globals.update(
    admin_url=admin_url, static_url=static_url, settings=settings, version=__version__, sparkline=sparkline,
    flag_emoji=flag_emoji,
)
templates.env.filters.update(
    fa=fa, dt=_fmt_dt, duration=fmt_duration, percent=fmt_percent, ago=fmt_ago, ms=_fmt_ms,
    error_label=error_label, status_label=lambda s: STATUS_LABELS.get(s, s),
)


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
