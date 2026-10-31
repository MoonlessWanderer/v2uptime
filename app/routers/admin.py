"""Admin panel.

Routes live under an internal prefix; :class:`app.main.AdminPathMiddleware`
maps the configurable public path (Setting ``admin_path``) onto it, so the
panel can move without a restart and the internal prefix is never reachable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from peewee import JOIN, fn
from starlette.datastructures import FormData, UploadFile

from app import __version__
from app.db import run_db
from app.models import Admin, Check, Incident, Server, Subscription
from app.security import (
    client_ip,
    csrf_token,
    csrf_valid,
    dummy_hash,
    hash_password,
    login_limiter,
    validate_password,
    verify_password,
)
from app.services import backup as backup_service
from app.services import servers as server_service
from app.services.checker import error_label
from app.services.control import ControlError
from app.services.parser import LinkParseError, parse_many
from app.services.settings import (
    ADVANCED_KEYS, BY_KEY, DEFINITIONS, SettingError, mask_secret, settings, validate,
)
from app.services.stats import RANGES, incident_stats
from app.services.subscriptions import validate_url
from app.services.telegram.outbox_worker import queue_stats
from app.timeutil import DAY, now_ts
from app.web import ADMIN_INTERNAL_PREFIX, admin_url, render

log = logging.getLogger(__name__)
router = APIRouter(prefix=ADMIN_INTERNAL_PREFIX, include_in_schema=False)


class NotAuthenticated(Exception):
    pass


# ---------------------------------------------------------------- helpers

def services(request: Request):
    return request.app.state.services


async def require_admin(request: Request) -> Admin:
    admin_id = request.session.get("admin_id")
    if admin_id:
        admin = await run_db(Admin.get_or_none, Admin.id == admin_id)
        if admin and request.session.get("pwd_at") == admin.password_changed_at:
            return admin
    request.session.pop("admin_id", None)
    raise NotAuthenticated()


def flash(request: Request, kind: str, message: str) -> None:
    items = list(request.session.get("flash", []))
    items.append([kind, message])
    request.session["flash"] = items[-8:]


def page(request: Request, template: str, admin: Admin | None, nav: str, **context: Any) -> Response:
    context.update(admin=admin, nav=nav, csrf=csrf_token(request), flashes=request.session.pop("flash", []))
    return render(request, f"admin/{template}", context)


def redirect(path: str = "") -> RedirectResponse:
    return RedirectResponse(admin_url(path), status_code=303)


async def checked_form(request: Request) -> FormData:
    form = await request.form()
    if not csrf_valid(request, form.get("csrf_token")):
        raise HTTPException(status_code=400, detail="Invalid security token. Reload the page and try again.")
    return form


async def checked_json(request: Request) -> dict[str, Any]:
    if not csrf_valid(request, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=400, detail="CSRF")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


def _bool(form: FormData, key: str) -> bool:
    return form.get(key) in ("1", "on", "true")


# ------------------------------------------------------------------ login

@router.get("/login")
async def login_page(request: Request):
    try:
        await require_admin(request)
        return redirect("/")
    except NotAuthenticated:
        pass
    return page(request, "login.html", None, "login", has_admin=await run_db(Admin.select().exists))


@router.post("/login")
async def login(request: Request):
    form = await checked_form(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    keys = [f"ip:{client_ip(request)}", f"user:{username.lower()}"]
    wait = login_limiter.retry_after(keys)
    if wait:
        flash(request, "error", f"Too many failed attempts. Try again in {wait // 60 + 1} minutes.")
        return redirect("/login")
    admin = await run_db(Admin.get_or_none, Admin.username == username)
    valid = await run_db(verify_password, password, admin.password_hash if admin else dummy_hash())
    if not admin or not valid:
        login_limiter.failure(keys)
        flash(request, "error", "Incorrect username or password.")
        return redirect("/login")
    login_limiter.reset(keys)
    request.session.clear()
    request.session.update(admin_id=admin.id, pwd_at=admin.password_changed_at, login_at=now_ts())
    return redirect("/")


@router.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    request.session.clear()
    return redirect("/login")


# -------------------------------------------------------------- dashboard

@router.get("/")
async def dashboard(request: Request):
    admin = await require_admin(request)
    svc = services(request)
    metrics = await run_db(svc.stats.server_metrics)
    names = await run_db(lambda: dict(Server.select(Server.id, Server.name).tuples()))
    statuses = {sid: svc.stats.current_status(sid) for sid in metrics}
    counts = {key: sum(1 for s in statuses.values() if s == key) for key in ("online", "degraded", "offline", "unknown")}
    problem = sorted(
        ({"id": sid, "name": names.get(sid, "?"), "status": st, "since": svc.incidents.open_incident_start(sid),
          "error": metrics[sid].last_error} for sid, st in statuses.items() if st in ("offline", "degraded")),
        key=lambda item: (item["status"] != "offline", item["name"]),
    )
    recent = await run_db(lambda: list(Incident.select(Incident, Server).join(Server)
                                       .order_by(Incident.started_at.desc()).limit(8)))
    report = svc.checker.last_report
    return page(request, "dashboard.html", admin, "dashboard",
                counts=counts, total=len(metrics), problem=problem, recent=recent, report=report,
                xray=svc.xray.status(), queue=await run_db(queue_stats), network_down=svc.monitor.network_down_since,
                telegram_enabled=settings.get("telegram_enabled"), xray_log=list(svc.xray.output)[-15:],
                bot_error=svc.bot.last_error, now=now_ts())


# ---------------------------------------------------------------- servers

def _server_rows(svc) -> list[dict[str, Any]]:
    metrics = svc.stats.server_metrics()
    rows = []
    for server in Server.select(Server, Subscription).join(Subscription, JOIN.LEFT_OUTER).order_by(
            Server.sort_order, Server.id):
        m = metrics.get(server.id)
        rows.append({
            "s": server, "status": svc.stats.current_status(server.id) if server.is_active else "unknown",
            "latency": m.latency_ms if m else None, "uptime": m.uptime_24h if m else None,
            "loaded": server.id in svc.xray.loaded_ports(),
        })
    return rows


@router.get("/servers")
async def servers_page(request: Request):
    admin = await require_admin(request)
    rows = await run_db(_server_rows, services(request))
    return page(request, "servers.html", admin, "servers", rows=rows, section="servers", tab="servers")


@router.post("/servers/add")
async def servers_add(request: Request):
    await require_admin(request)
    form = await checked_form(request)
    result = await run_db(server_service.add_links, str(form.get("links", "")), _bool(form, "is_public"),
                          str(form.get("tags", "")))
    if result.created:
        flash(request, "success", f"Added {len(result.created)} servers.")
        await services(request).after_servers_changed()
    if result.duplicates:
        flash(request, "warn", "Already exists (not added): " + ", ".join(result.duplicates[:10]))
    for line, excerpt, message in result.errors[:10]:
        flash(request, "error", f"Line {line}: {message} — {excerpt}")
    if not (result.created or result.duplicates or result.errors):
        flash(request, "error", "No links were entered.")
    return redirect("/servers")


@router.post("/servers/preview")
async def servers_preview(request: Request):
    await require_admin(request)
    body = await checked_json(request)
    bulk = parse_many(str(body.get("links", "")))
    return {
        "valid": [{"name": link.default_name, "protocol": link.protocol.upper()} for link in bulk.links],
        "invalid": [{"line": line, "message": message} for line, _excerpt, message in bulk.errors],
    }


@router.post("/servers/reorder")
async def servers_reorder(request: Request):
    await require_admin(request)
    body = await checked_json(request)
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
    await run_db(server_service.reorder, ids)
    services(request).stats.invalidate()
    return {"ok": True}


@router.get("/servers/{server_id}")
async def server_detail(request: Request, server_id: int):
    admin = await require_admin(request)
    svc = services(request)

    def load() -> dict[str, Any]:
        server = Server.get_or_none(Server.id == server_id)
        if server is None:
            return {}
        now = now_ts()
        stats = {key: incident_stats(now - seconds, now, server_id).get(server_id) for key, seconds in RANGES.items()}
        checks = list(Check.select().where(Check.server == server_id).order_by(Check.checked_at.desc()).limit(200))
        incidents = list(Incident.select().where(Incident.server == server_id)
                         .order_by(Incident.started_at.desc()).limit(50))
        connect_avg = (Check.select(fn.AVG(Check.connect_ms))
                       .where(Check.server == server_id, Check.checked_at >= now - DAY,
                              Check.connect_ms.is_null(False)).scalar())
        return {"server": server, "stats": stats, "checks": checks, "incidents": incidents,
                "metrics": svc.stats.server_metrics().get(server_id), "connect_avg": connect_avg}

    data = await run_db(load)
    if not data:
        raise HTTPException(404)
    port = svc.xray.loaded_ports().get(server_id)
    return page(request, "server_detail.html", admin, "servers", now=now_ts(), port=port,
                status=svc.stats.current_status(server_id), ranges=RANGES, **data)


@router.get("/servers/{server_id}/history.json")
async def server_history_json(request: Request, server_id: int, range: str = "24h"):
    await require_admin(request)
    if not await run_db(Server.select().where(Server.id == server_id).exists):
        raise HTTPException(404)
    return await run_db(services(request).stats.server_history, server_id, range)


@router.get("/servers/{server_id}/edit")
async def server_edit_page(request: Request, server_id: int):
    admin = await require_admin(request)
    server = await run_db(Server.get_or_none, Server.id == server_id)
    if server is None:
        raise HTTPException(404)
    return page(request, "server_form.html", admin, "servers", server=server)


@router.post("/servers/{server_id}/edit")
async def server_edit(request: Request, server_id: int):
    await require_admin(request)
    form = await checked_form(request)
    before = await run_db(Server.get_or_none, Server.id == server_id)
    if before is None:
        raise HTTPException(404)
    try:
        server = await run_db(lambda: server_service.update_server(
            server_id, name=str(form.get("name", "")), country=str(form.get("country", "")),
            tags=str(form.get("tags", "")), is_active=_bool(form, "is_active"), is_public=_bool(form, "is_public"),
            raw_link=str(form.get("raw_link", ""))))
    except (LinkParseError, ValueError) as exc:
        flash(request, "error", str(exc))
        return redirect(f"/servers/{server_id}/edit")
    await services(request).after_servers_changed([server_id] if before.is_active and not server.is_active else [])
    flash(request, "success", "Changes saved.")
    return redirect("/servers")


@router.post("/servers/{server_id}/delete")
async def server_delete(request: Request, server_id: int):
    await require_admin(request)
    await checked_form(request)
    await run_db(server_service.delete_server, server_id)
    await services(request).after_servers_changed([server_id])
    flash(request, "success", "Server deleted.")
    return redirect("/servers")


@router.post("/servers/{server_id}/toggle")
async def server_toggle(request: Request, server_id: int):
    await require_admin(request)
    body = await checked_json(request)
    flag = str(body.get("flag", ""))
    if flag not in ("is_active", "is_public"):
        raise HTTPException(400)
    server = await run_db(Server.get_or_none, Server.id == server_id)
    if server is None:
        raise HTTPException(404)
    value = not getattr(server, flag)
    await run_db(server_service.set_flag, server_id, flag, value)
    await services(request).after_servers_changed([server_id] if flag == "is_active" and not value else [])
    return {"ok": True, "flag": flag, "value": value}


@router.post("/servers/{server_id}/retry")
async def server_retry(request: Request, server_id: int):
    await require_admin(request)
    await checked_form(request)
    await run_db(server_service.clear_xray_error, server_id)
    await services(request).after_servers_changed()
    flash(request, "info", "Server was added to Xray again.")
    return redirect("/servers")


@router.post("/servers/{server_id}/test")
async def server_test(request: Request, server_id: int):
    await require_admin(request)
    await checked_json(request)
    result = await services(request).checker.test_now(server_id)
    return {"ok": result.ok, "latency_ms": result.latency_ms, "connect_ms": result.connect_ms,
            "error": error_label(result.error_type) if not result.ok else None,
            "detail": result.detail if not result.ok else None}


# ---------------------------------------------------------- subscriptions

@router.get("/subscriptions")
async def subscriptions_page(request: Request):
    admin = await require_admin(request)

    def load():
        subs = list(Subscription.select().order_by(Subscription.id))
        counts = {sid: n for sid, n in Server.select(Server.subscription, fn.COUNT(Server.id))
                  .where(Server.subscription.is_null(False)).group_by(Server.subscription).tuples()}
        return subs, counts

    subs, counts = await run_db(load)
    return page(request, "subscriptions.html", admin, "servers", subs=subs, counts=counts,
                section="servers", tab="subscriptions")


@router.post("/subscriptions/add")
async def subscription_add(request: Request):
    await require_admin(request)
    form = await checked_form(request)
    try:
        url = validate_url(str(form.get("url", "")))
        interval = max(10, min(int(form.get("interval_minutes") or 360), 10080))
    except ValueError as exc:
        flash(request, "error", str(exc) if "Subscription" in str(exc) else "Invalid sync interval.")
        return redirect("/subscriptions")
    sub = await run_db(Subscription.create, name=str(form.get("name", ""))[:200], url=url, interval_minutes=interval,
                       auto_sync=_bool(form, "auto_sync"), make_public=_bool(form, "make_public"))
    await _sync_and_flash(request, sub.id)
    return redirect("/subscriptions")


async def _sync_and_flash(request: Request, sub_id: int) -> None:
    try:
        r = await services(request).subscriptions.sync(sub_id)
        flash(request, "success", f"Sync finished ({r.via}): {r.added} added, {r.updated} updated, "
                                  f"{len(r.deactivated)} deactivated, {len(r.errors)} invalid links.")
    except (ValueError, LinkParseError) as exc:
        flash(request, "error", str(exc))


@router.post("/subscriptions/{sub_id}/sync")
async def subscription_sync(request: Request, sub_id: int):
    await require_admin(request)
    await checked_form(request)
    await _sync_and_flash(request, sub_id)
    return redirect("/subscriptions")


@router.post("/subscriptions/{sub_id}/edit")
async def subscription_edit(request: Request, sub_id: int):
    await require_admin(request)
    form = await checked_form(request)
    try:
        url = validate_url(str(form.get("url", "")))
        interval = max(10, min(int(form.get("interval_minutes") or 360), 10080))
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect("/subscriptions")
    await run_db(lambda: Subscription.update(
        name=str(form.get("name", ""))[:200], url=url, interval_minutes=interval, auto_sync=_bool(form, "auto_sync"),
        make_public=_bool(form, "make_public")).where(Subscription.id == sub_id).execute())
    flash(request, "success", "Subscription saved.")
    return redirect("/subscriptions")


@router.post("/subscriptions/{sub_id}/delete")
async def subscription_delete(request: Request, sub_id: int):
    await require_admin(request)
    form = await checked_form(request)
    delete_servers = _bool(form, "delete_servers")

    def remove() -> list[int]:
        ids = [s.id for s in Server.select(Server.id).where(Server.subscription == sub_id)]
        if delete_servers:
            Server.delete().where(Server.id.in_(ids)).execute()
        Subscription.delete_by_id(sub_id)
        return ids if delete_servers else []

    removed = await run_db(remove)
    await services(request).after_servers_changed(removed)
    flash(request, "success", "Subscription deleted" + (f" along with {len(removed)} servers." if removed else "."))
    return redirect("/subscriptions")


# -------------------------------------------------------------- incidents

@router.get("/incidents")
async def incidents_page(request: Request, server: int | None = None, range: str = "7d"):
    admin = await require_admin(request)
    range_key = range if range in RANGES else "7d"

    def load():
        now = now_ts()
        since = now - RANGES[range_key]
        query = (Incident.select(Incident, Server).join(Server)
                 .where((Incident.ended_at.is_null()) | (Incident.ended_at >= since))
                 .order_by(Incident.started_at.desc()).limit(300))
        if server:
            query = query.where(Incident.server == server)
        servers = list(Server.select().order_by(Server.sort_order, Server.id))
        per_range = {key: incident_stats(now - seconds, now) for key, seconds in RANGES.items()}
        return list(query), servers, per_range, now

    incidents, servers, per_range, now = await run_db(load)
    return page(request, "incidents.html", admin, "incidents", incidents=incidents, servers=servers,
                per_range=per_range, selected_server=server, range_key=range_key, ranges=RANGES, now=now)


# --------------------------------------------------------------- settings

def _partition(groups: list[str]) -> tuple[list[Any], list[Any]]:
    defs = [d for g in groups for d in DEFINITIONS if d.group == g and not d.internal]
    main = [d for d in defs if d.key not in ADVANCED_KEYS]
    advanced = [d for d in defs if d.key in ADVANCED_KEYS]
    return main, advanced


def _collect(form: FormData, keys: list[str]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    errors: list[str] = []
    for key in keys:
        d = BY_KEY[key]
        if d.kind == "bool":
            raw: Any = _bool(form, key)
        elif d.kind == "multi":
            raw = ",".join(str(v) for v in form.getlist(key))
        else:
            if key not in form:
                continue  # not part of this form: keep the stored value
            raw = form.get(key, "")
            if d.kind == "secret" and (not raw or raw == mask_secret(settings.get(key))):
                continue  # unchanged secret
        try:
            values[key] = validate(key, raw)
        except SettingError as exc:
            errors.append(str(exc))
    return values, errors


@router.get("/settings")
async def settings_page(request: Request):
    admin = await require_admin(request)
    general_main, general_adv = _partition(["general"])
    monitor_main, monitor_adv = _partition(["checks", "status", "retention"])
    return page(request, "settings.html", admin, "settings", values=settings.all(), section="settings", tab="general",
                general_main=general_main, general_adv=general_adv,
                monitor_main=monitor_main, monitor_adv=monitor_adv)


@router.post("/settings")
async def settings_save(request: Request):
    await require_admin(request)
    form = await checked_form(request)
    keys = [d.key for g in ("general", "checks", "status", "retention") for d in DEFINITIONS
            if d.group == g and not d.internal]
    values, errors = _collect(form, keys)
    if errors:
        for error in errors:
            flash(request, "error", error)
        return redirect("/settings")
    old_interval = settings.get("check_interval")
    await run_db(settings.set_many, values, True)
    svc = services(request)
    svc.stats.invalidate()
    if values.get("check_interval") != old_interval:
        svc.checker.wake()
    flash(request, "success", "Settings saved.")
    return redirect("/settings")


# --------------------------------------------------------------- telegram

@router.get("/telegram")
async def telegram_page(request: Request):
    admin = await require_admin(request)
    svc = services(request)
    servers = await run_db(lambda: list(Server.select().where(Server.is_active == True)  # noqa: E712
                                        .order_by(Server.sort_order, Server.id)))
    telegram_main, telegram_adv = _partition(["telegram"])
    return page(request, "telegram.html", admin, "settings",
                telegram_main=telegram_main, telegram_adv=telegram_adv,
                values=settings.all(), masked_token=mask_secret(settings.get("telegram_token")),
                servers=servers, queue=await run_db(queue_stats), bot_error=svc.bot.last_error,
                last_route=svc.sender.last_route_key, section="settings", tab="telegram")


@router.post("/telegram")
async def telegram_save(request: Request):
    await require_admin(request)
    form = await checked_form(request)
    keys = [d.key for d in DEFINITIONS if d.group == "telegram" and not d.internal]
    values, errors = _collect(form, keys)
    if errors:
        for error in errors:
            flash(request, "error", error)
        return redirect("/telegram")
    await run_db(settings.set_many, values, True)
    flash(request, "success", "Telegram settings saved.")
    return redirect("/telegram")


@router.post("/telegram/test")
async def telegram_test(request: Request):
    await require_admin(request)
    await checked_json(request)
    if not settings.get("telegram_token") or not settings.chat_ids():
        return JSONResponse({"ok": False, "results": [], "error": "Save a bot token and chat ID first."})
    results = await services(request).notifier.send_test()
    return {"ok": any(r["ok"] for r in results), "results": results}


# --------------------------------------------------------------- password

@router.get("/password")
async def password_page(request: Request):
    admin = await require_admin(request)
    return page(request, "password.html", admin, "settings", section="settings", tab="account")


@router.post("/password")
async def password_change(request: Request):
    admin = await require_admin(request)
    form = await checked_form(request)
    if not await run_db(verify_password, str(form.get("current", "")), admin.password_hash):
        flash(request, "error", "Current password is incorrect.")
        return redirect("/password")
    new = str(form.get("new", ""))
    error = validate_password(new, str(form.get("confirm", "")))
    if error:
        flash(request, "error", error)
        return redirect("/password")
    admin.password_hash = await run_db(hash_password, new)
    admin.password_changed_at = max(now_ts(), admin.password_changed_at + 1)
    await run_db(admin.save)
    request.session["pwd_at"] = admin.password_changed_at  # keep this session, invalidate others
    flash(request, "success", "Password changed.")
    return redirect("/password")


# ----------------------------------------------------------------- backup

@router.get("/backup")
async def backup_page(request: Request):
    admin = await require_admin(request)
    return page(request, "backup.html", admin, "settings", section="settings", tab="backup")


@router.get("/backup/export")
async def backup_export(request: Request):
    await require_admin(request)
    data = await run_db(backup_service.export_data)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    filename = f"v2uptime-backup-{now_ts()}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/backup/import")
async def backup_import(request: Request):
    await require_admin(request)
    form = await checked_form(request)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        flash(request, "error", "No file was selected.")
        return redirect("/backup")
    raw = await upload.read(20 * 1024 * 1024 + 1)
    try:
        data = json.loads(raw.decode("utf-8"))
        summary = await run_db(backup_service.import_data, data, form.get("mode") == "replace")
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash(request, "error", "The file is not valid JSON.")
        return redirect("/backup")
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect("/backup")
    svc = services(request)
    if form.get("mode") == "replace":
        await run_db(svc.incidents.restore)
    await svc.after_servers_changed()
    flash(request, "success", f"Import finished: {summary['servers']} servers, {summary['subscriptions']} subscriptions, "
                              f"{summary['settings']} settings ({summary['skipped']} skipped).")
    for error in summary["errors"][:10]:
        flash(request, "warn", error)
    return redirect("/backup")


# ------------------------------------------------------------ maintenance

def _maintenance_info(request: Request) -> dict[str, Any]:
    svc = services(request)
    return {"version": __version__, "now": now_ts(), "process_started_at": svc.started_at,
            "can_restart": svc.control.can_restart(), "helper": svc.control.helper(),
            "update": svc.control.update_status()}


@router.get("/maintenance")
async def maintenance_page(request: Request):
    admin = await require_admin(request)
    return page(request, "maintenance.html", admin, "settings", section="settings", tab="maintenance",
                info=_maintenance_info(request))


@router.get("/maintenance/status.json")
async def maintenance_status(request: Request):
    await require_admin(request)
    return JSONResponse(_maintenance_info(request), headers={"Cache-Control": "no-store"})


@router.post("/maintenance/update")
async def maintenance_update(request: Request):
    admin = await require_admin(request)
    await checked_json(request)
    try:
        requested_at = services(request).control.request_update()
    except ControlError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    log.info("panel update requested", extra={"admin": admin.username})
    return {"ok": True, "requested_at": requested_at}


@router.post("/maintenance/restart")
async def maintenance_restart(request: Request):
    admin = await require_admin(request)
    await checked_json(request)
    svc = services(request)
    try:
        svc.control.schedule_restart()
    except ControlError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    log.info("panel restart requested", extra={"admin": admin.username})
    return {"ok": True, "process_started_at": svc.started_at}
