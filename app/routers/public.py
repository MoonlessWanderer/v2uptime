"""Public status page (server-rendered; JS only swaps the live fragment)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db import run_db
from app.public_i18n import client_catalog, normalize_lang
from app.services.settings import settings
from app.services.stats import RANGES
from app.web import render

router = APIRouter()

LANG_COOKIE = "lang"


def _range(value: str | None) -> str:
    return value if value in RANGES else "24h"


def public_lang(request: Request) -> str:
    """The admin setting. ``?lang=`` and a ``lang`` cookie do not change it."""
    return normalize_lang(settings.get("public_language"))


def localized_status(data: dict, lang: str) -> dict:
    """Copy the public payload and, in Persian, overlay the optional Persian texts."""
    view = dict(data)
    site = dict(data.get("site") or {})
    if lang == "fa":
        for key, setting in (("title", "site_title_fa"), ("description", "site_description_fa")):
            value = (settings.get(setting) or "").strip()
            if value:
                site[key] = value
    view["site"] = site
    return view


def clear_lang_cookie(response, request: Request):
    """Drop a leftover ``lang`` cookie. Nothing writes a new one."""
    if LANG_COOKIE in request.cookies:
        response.delete_cookie(
            LANG_COOKIE, path="/",
            secure=request.app.state.config.cookie_secure, samesite="lax",
        )
    return response


async def _payload(request: Request, range_key: str) -> dict:
    return await run_db(request.app.state.services.stats.public_status, range_key)


def _context(request: Request, data: dict, range_key: str) -> dict:
    lang = public_lang(request)
    return {
        "data": localized_status(data, lang),
        "echo_range": range_key,
        "lang": lang,
        "dir": "rtl" if lang == "fa" else "ltr",
        "i18n": client_catalog(lang),
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, range: str | None = None):
    # The list is always the last 24 hours. ?range= is echoed for older clients and tests.
    data = await _payload(request, "24h")
    response = render(request, "public/index.html", _context(request, data, _range(range)))
    return clear_lang_cookie(response, request)


@router.get("/partials/live", response_class=HTMLResponse)
async def live_fragment(request: Request, range: str | None = None):
    data = await _payload(request, "24h")
    response = render(request, "public/_live.html", _context(request, data, _range(range)))
    response.headers["Cache-Control"] = "no-store"
    return clear_lang_cookie(response, request)


@router.get("/healthz")
async def healthz(request: Request):
    services = request.app.state.services
    return JSONResponse({"ok": True, "xray_ready": services.xray.is_ready()})
