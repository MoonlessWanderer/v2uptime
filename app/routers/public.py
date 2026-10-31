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
LANG_MAX_AGE = 365 * 24 * 3600


def _range(value: str | None) -> str:
    return value if value in RANGES else "24h"


def public_lang(request: Request) -> str:
    """URL, then the visitor's cookie, then the panel default."""
    query = request.query_params.get("lang")
    if query in ("en", "fa"):
        return query
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in ("en", "fa"):
        return cookie
    return normalize_lang(settings.get("public_language"))


def localized_status(data: dict, lang: str) -> dict:
    """Copy the public payload and, in Persian, overlay the optional Persian texts."""
    view = dict(data)
    site = dict(data.get("site") or {})
    if lang == "fa":
        for key, setting in (("title", "site_title_fa"), ("description", "site_description_fa"),
                             ("location", "test_location_fa")):
            value = (settings.get(setting) or "").strip()
            if value:
                site[key] = value
    view["site"] = site
    return view


def remember_lang(response, request: Request, lang: str):
    """Store the language when the visitor chose it with ?lang=."""
    if request.query_params.get("lang") in ("en", "fa"):
        response.set_cookie(
            LANG_COOKIE, lang, max_age=LANG_MAX_AGE, path="/",
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
    lang = public_lang(request)
    response = render(request, "public/index.html", _context(request, data, _range(range)))
    return remember_lang(response, request, lang)


@router.get("/partials/live", response_class=HTMLResponse)
async def live_fragment(request: Request, range: str | None = None):
    data = await _payload(request, "24h")
    lang = public_lang(request)
    response = render(request, "public/_live.html", _context(request, data, _range(range)))
    response.headers["Cache-Control"] = "no-store"
    return remember_lang(response, request, lang)


@router.get("/healthz")
async def healthz(request: Request):
    services = request.app.state.services
    return JSONResponse({"ok": True, "xray_ready": services.xray.is_ready()})
