"""JSON export / import of servers and settings."""

from __future__ import annotations

from typing import Any

from app import __version__
from app.db import db
from app.models import Server
from app.services.parser import parse_link
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
        "servers": [{
            "id": s.id, "name": s.name, "raw_link": s.raw_link, "country": s.country, "tags": s.tags,
            "is_active": s.is_active, "is_public": s.is_public, "sort_order": s.sort_order,
        } for s in Server.select().order_by(Server.sort_order, Server.id)],
    }


def import_data(data: dict[str, Any], replace: bool = False) -> dict[str, Any]:
    """Merge (default) or replace servers; settings are always merged.

    Older backups may contain ``subscriptions`` and ``subscription_id``. Both
    are ignored: those servers are imported as ordinary servers.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("Backup file is not valid.")
    if data.get("format_version", FORMAT_VERSION) != FORMAT_VERSION:
        raise ValueError("Unsupported backup version.")
    items = data.get("servers")
    saved_settings = data.get("settings", {})
    if not isinstance(items, list) or not isinstance(saved_settings, dict):
        raise ValueError("Backup must contain a server list and a settings object.")
    summary: dict[str, Any] = {"servers": 0, "skipped": 0, "settings": 0, "errors": []}
    prepared = []
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("Invalid server record.")
            parsed = parse_link(str(item.get("raw_link", "")))
            order = int(item.get("sort_order") or 0) or None
            for flag in ("is_public", "is_active"):
                if flag in item and not isinstance(item[flag], bool):
                    raise ValueError(f"{flag} must be true or false.")
            prepared.append((item, parsed, order))
        except (ValueError, TypeError) as exc:
            summary["errors"].append(str(exc))
    valid: dict[str, Any] = {}
    keys = {d.key for d in DEFINITIONS if not d.internal}
    for key, value in saved_settings.items():
        if key in keys:
            try:
                valid[key] = validate(key, "1" if value is True else "0" if value is False else str(value))
            except SettingError as exc:
                summary["errors"].append(str(exc))
    if replace and summary["errors"]:
        raise ValueError("Backup was not restored; existing data is unchanged. " + "; ".join(summary["errors"][:5]))
    with db.atomic():
        preferred = valid.get("telegram_preferred_server", settings.get("telegram_preferred_server"))
        old_preferred = Server.get_or_none(Server.id == preferred) if preferred else None
        preferred_identity = None
        for item, parsed, _order in prepared:
            if item.get("id") == preferred or (
                    "id" not in item and old_preferred and parsed.identity == old_preferred.identity):
                preferred_identity = parsed.identity
                break
        # Never reuse deleted IDs: a check already in flight belongs to the old server.
        next_id = (Server.select(Server.id).order_by(Server.id.desc()).scalar() or 0) + 1
        if replace:
            Server.delete().execute()
        identities = {i for (i,) in Server.select(Server.identity).tuples()}
        for item, parsed, order in prepared:
            if parsed.identity in identities:
                summary["skipped"] += 1
                continue
            server = create_from_parsed(parsed, is_public=bool(item.get("is_public", True)),
                                        tags=normalize_tags(str(item.get("tags", ""))),
                                        sort_order=order, server_id=next_id if replace else None)
            next_id += 1
            server.name = str(item.get("name") or server.name)[:200]
            server.country = str(item.get("country") or "")[:2].upper()
            server.is_active = bool(item.get("is_active", True))
            server.save()
            identities.add(parsed.identity)
            summary["servers"] += 1
        if replace or "telegram_preferred_server" in valid:
            target = Server.get_or_none(Server.identity == preferred_identity) if preferred_identity else None
            valid["telegram_preferred_server"] = target.id if target else 0
        if valid:
            settings.set_many(valid, validated=True)
            summary["settings"] = len(valid)
    return summary

