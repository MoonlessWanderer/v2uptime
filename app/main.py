"""FastAPI application: middleware, routers, static files, lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.config import Config, get_config
from app.db import init_db
from app.routers import admin, api, public
from app.services.container import Services
from app.services.settings import settings
from app.web import ADMIN_INTERNAL_PREFIX, STATIC_DIR, admin_url, render

log = logging.getLogger(__name__)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
     b"connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
]


class AdminPathMiddleware:
    """Serve the admin router (internal prefix) at the configurable ``admin_path``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            admin_path: str = settings.get("admin_path")
            if path == admin_path or path.startswith(admin_path + "/"):
                rest = path[len(admin_path):].rstrip("/") or "/"
                new_path = ADMIN_INTERNAL_PREFIX + rest
                scope = dict(scope, path=new_path, raw_path=new_path.encode())
            elif path.startswith(ADMIN_INTERNAL_PREFIX):
                response = JSONResponse({"detail": "Not Found"}, status_code=404)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [h for h in SECURITY_HEADERS if h[0] not in names]
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(cfg: Config | None = None, background: bool = True) -> FastAPI:
    cfg = cfg or get_config()
    init_db(cfg.db_path)
    settings.load()
    services = Services(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await services.start(background=background)
        log.info("panel started", extra={"version": __version__, "listen": f"{cfg.host}:{cfg.port}",
                                         "admin_path": settings.get("admin_path")})
        try:
            yield
        finally:
            await services.stop()

    app = FastAPI(title="v2uptime", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False)
    app.state.services = services
    app.state.config = cfg

    # Order matters: the outermost middleware is added last.
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="v2u_session",
                       max_age=7 * 86400, same_site="lax", https_only=cfg.cookie_secure)
    app.add_middleware(AdminPathMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(api.router)
    app.include_router(admin.router)

    @app.exception_handler(admin.NotAuthenticated)
    async def not_authenticated(request: Request, exc: admin.NotAuthenticated):
        wants_json = (request.method != "GET" and "application/json" in request.headers.get("content-type", "")) \
            or "application/json" in request.headers.get("accept", "")
        if wants_json:
            return JSONResponse({"detail": "Your session has expired. Sign in again."}, status_code=401)
        return RedirectResponse(admin_url("/login"), status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/internal/") or "application/json" in \
                request.headers.get("accept", "") or "application/json" in request.headers.get("content-type", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else "Page not found."
        return render(request, "error.html", {"code": exc.status_code, "message": message},
                      status_code=exc.status_code)

    return app

