"""JSON export / import of servers, subscriptions and settings."""

from __future__ import annotations

from typing import Any

from app import __version__
from app.db import db
from app.models import Server, Subscription
from app.services.parser import LinkParseError, parse_link
from app.services.servers import create_from_parsed, normalize_tags
from app.services.settings import DEFINITIONS, SettingError, settings, validate
from app.timeutil import now_ts

FORMAT = "v2uptime-backup"
FORMAT_VERSION = 1


def export_data() -> dict[str, Any]:
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "exported_at": now_ts(),
        "settings": {d.key: settings.get(d.key) for d in DEFINITIONS if not d.internal},
        "subscriptions": [{
            "id": s.id, "name": s.name, "url": s.url, "interval_minutes": s.interval_minutes,
            "auto_sync": s.auto_sync, "make_public": s.make_public,
        } for s in Subscription.select().order_by(Subscription.id)],
        "servers": [{
            "name": s.name, "raw_link": s.raw_link, "country": s.country, "tags": s.tags,
            "is_active": s.is_active, "is_public": s.is_public, "sort_order": s.sort_order,
            "subscription_id": s.subscription_id,
        } for s in Server.select().order_by(Server.sort_order, Server.id)],
    }


def import_data(data: dict[str, Any], replace: bool = False) -> dict[str, Any]:
    """Merge (default) or replace servers/subscriptions; settings are always merged."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("فایل پشتیبان معتبر نیست.")
    summary: dict[str, Any] = {"servers": 0, "skipped": 0, "subscriptions": 0, "settings": 0, "errors": []}
    with db.atomic():
        if replace:
            Server.delete().execute()
            Subscription.delete().execute()
        sub_map: dict[int, int] = {}
        existing_urls = {s.url: s.id for s in Subscription.select()}
        for item in data.get("subscriptions", []):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            if url in existing_urls:
                sub_map[item.get("id")] = existing_urls[url]
                continue
            sub = Subscription.create(name=str(item.get("name", ""))[:200], url=url,
                                      interval_minutes=int(item.get("interval_minutes") or 360),
                                      auto_sync=bool(item.get("auto_sync", True)),
                                      make_public=bool(item.get("make_public", True)))
            sub_map[item.get("id")] = sub.id
            existing_urls[url] = sub.id
            summary["subscriptions"] += 1
        identities = {i for (i,) in Server.select(Server.identity).tuples()}
        for item in data.get("servers", []):
            try:
                parsed = parse_link(str(item.get("raw_link", "")))
            except LinkParseError as exc:
                summary["errors"].append(f"{item.get('name', '?')}: {exc}")
                continue
            if parsed.identity in identities:
                summary["skipped"] += 1
                continue
            server = create_from_parsed(parsed, is_public=bool(item.get("is_public", True)),
                                        tags=normalize_tags(str(item.get("tags", ""))),
                                        subscription_id=sub_map.get(item.get("subscription_id")),
                                        sort_order=int(item.get("sort_order") or 0) or None)
            server.name = str(item.get("name") or server.name)[:200]
            server.country = str(item.get("country") or "")[:2].upper()
            server.is_active = bool(item.get("is_active", True))
            server.save()
            identities.add(parsed.identity)
            summary["servers"] += 1
    valid: dict[str, Any] = {}
    keys = {d.key for d in DEFINITIONS if not d.internal}
    for key, value in (data.get("settings") or {}).items():
        if key not in keys:
            continue
        try:
            valid[key] = validate(key, "1" if value is True else "0" if value is False else str(value))
        except SettingError as exc:
            summary["errors"].append(str(exc))
    if valid:
        settings.set_many(valid, validated=True)
        summary["settings"] = len(valid)
    return summary

