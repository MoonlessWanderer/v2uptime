"""Read-only public JSON API and the localhost-only internal endpoint.

Public responses never contain share links, addresses or ports.  Servers that
are hidden (``is_public=False``) or inactive do not exist for this API.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.db import run_db
from app.models import Server
from app.services.stats import RANGES

router = APIRouter()

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _range(value: str | None) -> str:
    return value if value in RANGES else "24h"


def _is_public(server_id: int) -> bool:
    return Server.select().where(Server.id == server_id, Server.is_active == True,  # noqa: E712
                                 Server.is_public == True).exists()  # noqa: E712


@router.get("/api/status")
async def api_status(request: Request, range: str | None = None):
    data = await run_db(request.app.state.services.stats.public_status, _range(range))
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=30"})


@router.get("/api/servers/{server_id}/history")
async def api_history(request: Request, server_id: int, range: str | None = None):
    if not await run_db(_is_public, server_id):
        raise HTTPException(status_code=404, detail="سرور پیدا نشد")
    data = await run_db(request.app.state.services.stats.server_history, server_id, _range(range))
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=30"})


def _is_local_request(request: Request) -> bool:
    """Direct connection from this machine, not proxied through nginx."""
    client = request.client.host if request.client else ""
    return client in LOCAL_HOSTS and not any(h in request.headers for h in FORWARD_HEADERS)


@router.get("/internal/proxy")
async def internal_proxy(request: Request, format: str = "json", exclude: int | None = None):
    """SOCKS address of a healthy server, for install.sh (updates through a tunnel)."""
    if not _is_local_request(request):
        raise HTTPException(status_code=404)
    pool = request.app.state.services.proxy_pool
    candidates = await pool.acandidates([exclude] if exclude else [])
    if not candidates:
        if format == "text":
            return PlainTextResponse("", status_code=503)
        return JSONResponse({"proxy": None, "error": "no healthy server"}, status_code=503)
    best = candidates[0]
    if format == "text":
        return PlainTextResponse(best.url + "\n")
    return {"proxy": best.url, "server_id": best.server_id, "name": best.name, "status": best.status,
            "alternatives": [c.url for c in candidates[1:4]]}
