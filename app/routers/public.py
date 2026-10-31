"""Public status page (server-rendered; JS only swaps the live fragment)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db import run_db
from app.services.stats import RANGE_LABELS, RANGES
from app.web import render

router = APIRouter()


def _range(value: str | None) -> str:
    return value if value in RANGES else "24h"


async def _payload(request: Request, range_key: str) -> dict:
    return await run_db(request.app.state.services.stats.public_status, range_key)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, range: str | None = None):
    range_key = _range(range)
    data = await _payload(request, range_key)
    return render(request, "public/index.html", {"data": data, "ranges": RANGE_LABELS})


@router.get("/partials/live", response_class=HTMLResponse)
async def live_fragment(request: Request, range: str | None = None):
    data = await _payload(request, _range(range))
    response = render(request, "public/_live.html", {"data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/healthz")
async def healthz(request: Request):
    services = request.app.state.services
    return JSONResponse({"ok": True, "xray_ready": services.xray.is_ready()})
