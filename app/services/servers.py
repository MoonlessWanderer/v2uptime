"""Server management (sync DB functions; call through ``run_db``)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from peewee import fn

from app.db import db
from app.models import Check, HourlyStat, Incident, Server
from app.services.parser import LinkParseError, ParsedLink, parse_link, parse_many
from app.timeutil import now_ts


@dataclass
class AddResult:
    created: list[int] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[tuple[int, str, str]] = field(default_factory=list)


def normalize_tags(text: str) -> str:
    tags = [t.strip()[:30] for t in re.split(r"[,\u060c]", text or "") if t.strip()]
    return ",".join(dict.fromkeys(tags))[:500]


def _next_sort_order() -> int:
    return (Server.select(fn.MAX(Server.sort_order)).scalar() or 0) + 1


def apply_parsed(server: Server, parsed: ParsedLink) -> None:
    server.raw_link = parsed.raw
    server.protocol = parsed.protocol
    server.transport = parsed.network
    server.security = parsed.security
    server.address = parsed.address
    server.port = parsed.port
    server.parsed_outbound = json.dumps(parsed.outbound, ensure_ascii=False)
    server.identity = parsed.identity
    server.xray_error = None


def create_from_parsed(parsed: ParsedLink, *, is_public: bool = True, tags: str = "",
                       sort_order: int | None = None) -> Server:
    server = Server(name=parsed.default_name, country=parsed.country, tags=normalize_tags(tags),
                    is_public=is_public,
                    sort_order=sort_order if sort_order is not None else _next_sort_order())
    apply_parsed(server, parsed)
    server.save()
    return server


def add_links(text: str, is_public: bool = True, tags: str = "") -> AddResult:
    bulk = parse_many(text)
    result = AddResult(errors=bulk.errors)
    existing = {identity for (identity,) in Server.select(Server.identity).tuples()}
    order = _next_sort_order()
    with db.atomic():
        for parsed in bulk.links:
            if parsed.identity in existing:
                result.duplicates.append(parsed.default_name)
                continue
            server = create_from_parsed(parsed, is_public=is_public, tags=tags, sort_order=order)
            existing.add(parsed.identity)
            result.created.append(server.id)
            order += 1
    return result


def update_server(server_id: int, *, name: str, country: str, tags: str, is_active: bool, is_public: bool,
                  raw_link: str) -> Server:
    """Raises LinkParseError or ValueError with a message safe to show the admin."""
    server = Server.get_by_id(server_id)
    name = (name or "").strip()
    if not name:
        raise ValueError("Server name cannot be empty.")
    country = (country or "").strip().upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Country code must be two letters (for example DE).")
    raw_link = (raw_link or "").strip()
    if raw_link and raw_link != server.raw_link:
        parsed = parse_link(raw_link)
        duplicate = Server.select().where(Server.identity == parsed.identity, Server.id != server.id).exists()
        if duplicate:
            raise ValueError("Another server already uses this address, port, and identity.")
        apply_parsed(server, parsed)
    server.name = name[:200]
    server.country = country
    server.tags = normalize_tags(tags)
    server.is_active = is_active
    server.is_public = is_public
    server.updated_at = now_ts()
    server.save()
    return server


def set_flag(server_id: int, flag: str, value: bool) -> Server:
    if flag not in ("is_active", "is_public"):
        raise ValueError("invalid flag")
    server = Server.get_by_id(server_id)
    setattr(server, flag, value)
    server.updated_at = now_ts()
    server.save()
    return server


def reorder(ids: list[int]) -> None:
    with db.atomic():
        for position, server_id in enumerate(ids, start=1):
            Server.update(sort_order=position).where(Server.id == server_id).execute()


def delete_server(server_id: int) -> None:
    Server.delete_by_id(server_id)


def bulk_apply(action: str, ids: list[int]) -> tuple[int, list[int]]:
    """Apply one action to the given servers in a single transaction.

    Unknown ids are ignored. Returns ``(changed, removed_ids)``. ``removed_ids``
    is every deleted server, so the caller can drop them from Xray once.
    """
    wanted = list(dict.fromkeys(ids))
    if action not in ("delete", "public", "hidden") or not wanted:
        return 0, []
    found = list(Server.select().where(Server.id.in_(wanted)))
    found_ids = [server.id for server in found]
    if not found_ids:
        return 0, []
    with db.atomic():
        if action == "delete":
            Check.delete().where(Check.server.in_(found_ids)).execute()
            HourlyStat.delete().where(HourlyStat.server.in_(found_ids)).execute()
            Incident.delete().where(Incident.server.in_(found_ids)).execute()
            changed = Server.delete().where(Server.id.in_(found_ids)).execute()
            return changed, found_ids
        value = action == "public"
        changed = (Server.update(is_public=value, updated_at=now_ts())
                   .where(Server.id.in_(found_ids), Server.is_public != value).execute())
        return changed, []


def clear_xray_error(server_id: int) -> None:
    Server.update(xray_error=None).where(Server.id == server_id).execute()


__all__ = ["AddResult", "LinkParseError", "add_links", "apply_parsed", "create_from_parsed", "update_server",
           "set_flag", "reorder", "delete_server", "bulk_apply", "clear_xray_error", "normalize_tags"]
