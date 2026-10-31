"""Turn a paste of share links and subscription URLs into ordinary servers.

A subscription URL is fetched once (directly, then through up to three online
servers). Its configs are added like any other server. The URL is not stored
and is not synced again. Preview results are remembered for five minutes so
saving does not download the same URL twice.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.db import db, run_db
from app.models import Server
from app.services import net
from app.services.parser import (
    BulkResult,
    LinkParseError,
    ParsedLink,
    decode_subscription,
    parse_link,
    parse_many,
)
from app.services.proxy_pool import ProxyPool
from app.services.servers import create_from_parsed, normalize_tags

USER_AGENT = "v2rayN/7.0"
MAX_SIZE = 5 * 1024 * 1024
MAX_SUBSCRIPTION_LINKS = 10
CACHE_SECONDS = 300

_cache: dict[str, tuple[float, str]] = {}


class ImportError(Exception):
    """A subscription URL could not be used. ``host`` never includes the path."""

    def __init__(self, host: str, reason: str) -> None:
        self.host = host or "url"
        self.reason = reason
        super().__init__(f"{self.host}: {reason}")


@dataclass
class SubSummary:
    url: str
    host: str
    configs: int = 0
    new: int = 0
    duplicates: int = 0
    invalid: int = 0
    error: str | None = None


@dataclass
class Prepared:
    to_add: list[ParsedLink] = field(default_factory=list)
    share_errors: list[tuple[int, str, str]] = field(default_factory=list)
    share_valid: list[ParsedLink] = field(default_factory=list)
    subscriptions: list[SubSummary] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    from_subscriptions: int = 0
    subscription_ok: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    too_many: bool = False


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "url").strip("[]")


def is_subscription_url(line: str) -> bool:
    parts = urlsplit((line or "").strip())
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _resolve(host: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(host, None)})


def assert_not_local(url: str, resolver=None) -> None:
    """Reject loopback and link-local targets, including names that resolve to them."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").strip("[]")
    if parts.scheme not in ("http", "https") or not host:
        raise ImportError(host or "url", "must start with http:// or https://")
    if host.lower() == "localhost":
        raise ImportError("localhost", "local address")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        lookup = resolver or _resolve
        try:
            found = lookup(host)
        except OSError:
            raise ImportError(host, "could not resolve") from None
        if not found:
            raise ImportError(host, "could not resolve")
        try:
            addresses = [ipaddress.ip_address(item) for item in found]
        except ValueError:
            raise ImportError(host, "could not resolve") from None
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_loopback or address.is_link_local or (
                mapped is not None and (mapped.is_loopback or mapped.is_link_local)):
            raise ImportError(host, "local address")


def cached_content(url: str) -> str | None:
    hit = _cache.get(url)
    if hit is None:
        return None
    stored, content = hit
    if time.monotonic() - stored > CACHE_SECONDS:
        _cache.pop(url, None)
        return None
    return content


def store_content(url: str, content: str) -> None:
    _cache[url] = (time.monotonic(), content)


def clear_cache() -> None:
    _cache.clear()


async def fetch_text(url: str, proxy_pool: ProxyPool | None, timeout: float = 20.0,
                     transport: httpx.BaseTransport | None = None, resolver=None) -> str:
    """Return the body. Tries a direct request first, then up to three servers."""
    host = host_of(url)
    routes: list[tuple[str | None, str]] = [(None, "direct")]
    if proxy_pool is not None and transport is None:
        routes += [(item.url, f"server {item.name}") for item in (await proxy_pool.acandidates())[:3]]
    reason = "could not fetch"

    async def guard(request: httpx.Request) -> None:
        assert_not_local(str(request.url), resolver)

    for proxy, _label in routes:
        try:
            kwargs = {
                "timeout": httpx.Timeout(timeout, connect=8),
                "follow_redirects": True,
                "headers": {"User-Agent": USER_AGENT},
                "event_hooks": {"request": [guard]},
            }
            client = (httpx.AsyncClient(transport=transport, trust_env=False, **kwargs) if transport is not None
                      else net.async_client(proxy, **kwargs))
            async with client as http:
                response = await http.get(url)
            if response.status_code != 200:
                reason = f"HTTP {response.status_code}"
                continue
            if len(response.content) > MAX_SIZE:
                raise ImportError(host, "content is too large")
            return response.text
        except ImportError:
            raise
        except httpx.TimeoutException:
            reason = "timeout"
        except (httpx.HTTPError, OSError):
            reason = "could not fetch"
    raise ImportError(host, reason)


def _parse_shares(lines: list[tuple[int, str]]) -> BulkResult:
    links: list[ParsedLink] = []
    errors: list[tuple[int, str, str]] = []
    for number, line in lines:
        raw = line.strip()
        if not raw or raw.startswith("//") or raw.startswith(";"):
            continue
        try:
            links.append(parse_link(raw))
        except LinkParseError as exc:
            excerpt = raw if len(raw) <= 60 else raw[:57] + "..."
            errors.append((number, excerpt, str(exc)))
    return BulkResult(links, errors)


def _split(text: str) -> tuple[list[tuple[int, str]], list[str], bool]:
    """Share lines, subscription URLs, and whether the paste is a bare Base64 blob."""
    raw = text or ""
    stripped = raw.strip()
    numbered = list(enumerate(raw.splitlines(), start=1))
    urls = [line.strip() for _number, line in numbered if is_subscription_url(line)]
    if urls or "://" in stripped:
        shares = [(number, line) for number, line in numbered if not is_subscription_url(line)]
        return shares, urls, False
    return [], [], True


def _take(parsed: ParsedLink, seen: set[str], duplicates: list[str]) -> bool:
    if parsed.identity in seen:
        duplicates.append(parsed.default_name)
        return False
    seen.add(parsed.identity)
    return True


async def prepare(text: str, proxy_pool: ProxyPool | None = None, *,
                  transport: httpx.BaseTransport | None = None, resolver=None,
                  use_cache: bool = True) -> Prepared:
    """Fetch subscription URLs and decide what would be added. Does not write servers."""
    shares, urls, blob = _split(text)
    prepared = Prepared()
    if len(urls) > MAX_SUBSCRIPTION_LINKS:
        prepared.too_many = True
        return prepared

    if blob:
        bulk = parse_many(text)
        prepared.share_valid = list(bulk.links)
        prepared.share_errors = list(bulk.errors)
    else:
        bulk = _parse_shares(shares)
        prepared.share_valid = list(bulk.links)
        prepared.share_errors = list(bulk.errors)

    def existing_identities() -> set[str]:
        return {identity for (identity,) in Server.select(Server.identity).tuples()}

    seen = set(await run_db(existing_identities))
    for parsed in prepared.share_valid:
        if _take(parsed, seen, prepared.duplicates):
            prepared.to_add.append(parsed)

    for url in urls:
        host = host_of(url)
        try:
            assert_not_local(url, resolver)
            content = cached_content(url) if use_cache else None
            if content is None:
                content = await fetch_text(url, proxy_pool, transport=transport, resolver=resolver)
                if use_cache:
                    store_content(url, content)
            decoded = decode_subscription(content)
            inner = parse_many(decoded)
        except ImportError as exc:
            prepared.subscriptions.append(SubSummary(url=url, host=exc.host, error=exc.reason))
            prepared.failed.append((exc.host, exc.reason))
            continue
        except LinkParseError as exc:
            prepared.subscriptions.append(SubSummary(url=url, host=host, error=str(exc)))
            prepared.failed.append((host, str(exc)))
            continue
        summary = SubSummary(url=url, host=host, configs=len(inner.links), invalid=len(inner.errors))
        for parsed in inner.links:
            if _take(parsed, seen, prepared.duplicates):
                prepared.to_add.append(parsed)
                prepared.from_subscriptions += 1
                summary.new += 1
            else:
                summary.duplicates += 1
        prepared.subscriptions.append(summary)
        prepared.subscription_ok += 1
    return prepared


def commit(links: list[ParsedLink], is_public: bool = True, tags: str = "") -> list[int]:
    """Insert ``links`` that are not already stored. One transaction."""
    created: list[int] = []
    clean_tags = normalize_tags(tags)
    with db.atomic():
        seen = {identity for (identity,) in Server.select(Server.identity).tuples()}
        order = (Server.select(Server.sort_order).order_by(Server.sort_order.desc()).scalar() or 0) + 1
        for parsed in links:
            if parsed.identity in seen:
                continue
            server = create_from_parsed(parsed, is_public=is_public, tags=clean_tags, sort_order=order)
            seen.add(parsed.identity)
            created.append(server.id)
            order += 1
    return created


def result_message(prepared: Prepared, created: int) -> str:
    parts: list[str] = []
    if created:
        text = f"Added {created} server" if created == 1 else f"Added {created} servers"
        if prepared.from_subscriptions:
            word = "link" if prepared.subscription_ok == 1 else "links"
            text += f" ({prepared.from_subscriptions} from {prepared.subscription_ok} subscription {word})"
        parts.append(text + ".")
    if prepared.duplicates:
        count = len(prepared.duplicates)
        parts.append(f"{count} duplicate skipped." if count == 1 else f"{count} duplicates skipped.")
    if prepared.failed:
        word = "link" if len(prepared.failed) == 1 else "links"
        detail = ", ".join(f"{host} ({reason})" for host, reason in prepared.failed)
        parts.append(f"{len(prepared.failed)} {word} failed: {detail}.")
    return " ".join(parts)
