"""Resolve config domains with the admin's DNS chain.

Xray uses the same chain (primary, then secondary). Python repeats it only
after a domain server's check has already failed, so a DNS problem is stored
as ``dns`` or ``dns_blocked`` instead of a generic connection error. Each
name is cached for at most 60 seconds.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import dns.exception
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver

from app.services.settings import SettingError
from app.timeutil import now_ts

FILTERED_NET = ipaddress.ip_network("10.10.34.0/24")
UNEXPECTED_IPS = ["10.10.34.0/24"]
DNS_TAG = "dns-query"
DIRECT_TAG = "direct"
SAMPLE_NAME = "www.google.com"
CACHE_SECONDS = 60
QUERY_TIMEOUT = 3

_FORMAT = (
    'Use an IP (8.8.8.8), IP:port (8.8.8.8:53), tcp://1.1.1.1, '
    'https://1.1.1.1/dns-query, or localhost.'
)

_cache: dict[tuple, tuple[float, "Resolution"]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class Resolution:
    ips: list[str]
    source: str  # primary, secondary, system, or ""
    at: int
    error: str | None = None
    blocked: bool = False
    elapsed_ms: int = 0


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").strip("[]"))
    except ValueError:
        return False
    return True


def parse_dns(raw: str) -> str:
    """Normalise one DNS server, or '' when the field is empty."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.lower() == "localhost":
        return "localhost"
    if text.lower().startswith("https://"):
        return _parse_doh(text)
    if text.lower().startswith("tcp://"):
        host, port = _host_port(text[6:])
        if not is_ip(host):
            raise SettingError(
                "tcp:// DNS must use an IP address, for example tcp://1.1.1.1 or tcp://1.1.1.1:53."
            )
        return _format_ip(host, port, "tcp://")
    if "://" in text:
        raise SettingError(_FORMAT)
    host, port = _host_port(text)
    if not is_ip(host):
        raise SettingError(_FORMAT)
    return _format_ip(host, port, "")


def xray_server(spec: str) -> dict:
    """One Xray DnsServerObject. unexpectedIPs needs Xray 25.6.8 or newer."""
    item: dict = {"address": spec, "timeoutMs": 3000, "unexpectedIPs": list(UNEXPECTED_IPS)}
    if spec == "localhost" or spec.startswith(("https://", "tcp://")):
        return item
    host, port = _host_port(spec)
    item["address"] = f"[{host}]" if ":" in host else host
    if port != 53:
        item["port"] = port
    return item


def dns_section(servers: list[str], ipv4_only: bool) -> dict:
    section: dict = {"tag": DNS_TAG, "servers": [xray_server(s) for s in servers]}
    if ipv4_only:
        section["queryStrategy"] = "UseIPv4"
    return section


def xray_dns_options() -> tuple[list[str], bool, str | None]:
    """(servers, ipv4_only, domain strategy) from settings. Strategy is None when DNS is off."""
    from app.services.settings import settings

    servers = [s for s in (settings.get("dns_primary"), settings.get("dns_secondary")) if s]
    ipv4_only = bool(settings.get("dns_ipv4_only"))
    if not servers:
        return [], False, None
    return servers, ipv4_only, ("ForceIPv4" if ipv4_only else "ForceIP")


def resolve(name: str) -> Resolution:
    """Resolve ``name`` with the configured chain. Cached for 60 seconds."""
    chain, ipv4_only = _chain()
    key = (name.lower(), tuple(chain), ipv4_only)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    answer = _resolve_uncached(name, chain, ipv4_only)
    with _cache_lock:
        _cache[key] = (time.monotonic(), answer)
    return answer


def failure_kind(address: str) -> str | None:
    """``dns``, ``dns_blocked``, or None when the name resolves to a usable address."""
    if not address or is_ip(address):
        return None
    answer = resolve(address)
    if answer.ips:
        return None
    if answer.blocked:
        return "dns_blocked"
    return "dns"


def test_one(spec: str, ipv4_only: bool, name: str = SAMPLE_NAME) -> dict:
    """Resolve the sample name with one server. Used by the Test DNS button."""
    started = time.perf_counter()
    try:
        ips = query_records(spec, name, ipv4_only)
    except Exception as exc:  # noqa: BLE001 - shown to the admin as the test result
        return {"ok": False, "error": _short(exc), "ms": _ms(started)}
    good, only_filtered = _split(ips)
    if good:
        return {"ok": True, "ips": good, "ms": _ms(started)}
    if only_filtered:
        return {"ok": False, "error": "filtered answer (10.10.34.0/24)", "ms": _ms(started)}
    return {"ok": False, "error": "no answer", "ms": _ms(started)}


def query_records(spec: str, name: str, ipv4_only: bool) -> list[str]:
    """A and (unless IPv4-only) AAAA records from one server. Raises on transport errors."""
    if spec == "localhost":
        return _system_records(name, ipv4_only)
    kinds = [dns.rdatatype.A] if ipv4_only else [dns.rdatatype.A, dns.rdatatype.AAAA]
    ips: list[str] = []
    for kind in kinds:
        try:
            response = _exchange(dns.message.make_query(name, kind), spec)
        except TimeoutError:
            if ips:
                return ips
            raise
        if response.rcode() != dns.rcode.NOERROR:
            continue
        for rrset in response.answer:
            for item in rrset:
                address = getattr(item, "address", None)
                if address and address not in ips:
                    ips.append(address)
    return ips


# ---------------------------------------------------------------- internals

def _chain() -> tuple[list[tuple[str, str]], bool]:
    from app.services.settings import settings

    chain: list[tuple[str, str]] = []
    if settings.get("dns_primary"):
        chain.append(("primary", settings.get("dns_primary")))
    if settings.get("dns_secondary"):
        chain.append(("secondary", settings.get("dns_secondary")))
    if not chain:
        return [("system", "localhost")], False
    return chain, bool(settings.get("dns_ipv4_only"))


def _resolve_uncached(name: str, chain: list[tuple[str, str]], ipv4_only: bool) -> Resolution:
    started = time.perf_counter()
    last_error = "no answer"
    blocked_source = ""
    for label, spec in chain:
        try:
            ips = query_records(spec, name, ipv4_only)
        except Exception as exc:  # noqa: BLE001 - try the next server
            last_error = _short(exc)
            continue
        good, only_filtered = _split(ips)
        if good:
            return Resolution(good, _source(label, spec), now_ts(), elapsed_ms=_ms(started))
        if only_filtered:
            blocked_source = _source(label, spec)
            last_error = "filtered answer"
            continue
        last_error = "no answer"
    if blocked_source:
        return Resolution([], blocked_source, now_ts(), error="filtered answer", blocked=True, elapsed_ms=_ms(started))
    return Resolution([], "", now_ts(), error=last_error, elapsed_ms=_ms(started))


def _source(label: str, spec: str) -> str:
    if spec == "localhost":
        return "system"
    return label


def _split(ips: list[str]) -> tuple[list[str], bool]:
    good: list[str] = []
    saw_filtered = False
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr in FILTERED_NET:
            saw_filtered = True
            continue
        if ip not in good:
            good.append(ip)
    return good, saw_filtered and not good


def _system_records(name: str, ipv4_only: bool) -> list[str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = QUERY_TIMEOUT
    resolver.timeout = QUERY_TIMEOUT
    kinds = ["A"] if ipv4_only else ["A", "AAAA"]
    ips: list[str] = []
    for kind in kinds:
        try:
            answers = resolver.resolve(name, kind, raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            continue
        except dns.exception.Timeout as exc:
            raise TimeoutError("timeout") from exc
        for item in answers:
            address = getattr(item, "address", None)
            if address and address not in ips:
                ips.append(address)
    return ips


def _exchange(query: dns.message.Message, spec: str) -> dns.message.Message:
    try:
        if spec.startswith("https://"):
            parts = urlsplit(spec)
            host = parts.hostname or ""
            kwargs = {"port": parts.port or 443, "path": parts.path or "/dns-query", "timeout": QUERY_TIMEOUT}
            if is_ip(host):
                kwargs["bootstrap_address"] = host
            return dns.query.https(query, host, **kwargs)
        if spec.startswith("tcp://"):
            host, port = _host_port(spec[6:])
            return dns.query.tcp(query, host, port=port, timeout=QUERY_TIMEOUT)
        host, port = _host_port(spec)
        return dns.query.udp(query, host, port=port, timeout=QUERY_TIMEOUT)
    except dns.exception.Timeout as exc:
        raise TimeoutError("timeout") from exc


def _parse_doh(text: str) -> str:
    parts = urlsplit(text)
    if not parts.hostname:
        raise SettingError("DoH must be an https URL, preferably with an IP (https://1.1.1.1/dns-query).")
    path = parts.path or "/dns-query"
    if path == "/":
        path = "/dns-query"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = f":{parts.port}" if parts.port else ""
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{port}{path}{query}"


def _host_port(text: str) -> tuple[str, int]:
    text = text.strip()
    if not text:
        raise SettingError(_FORMAT)
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise SettingError(_FORMAT)
        host = text[1:end]
        rest = text[end + 1:]
        if rest == "":
            return host, 53
        if rest.startswith(":") and rest[1:].isdigit():
            return host, _port(rest[1:])
        raise SettingError(_FORMAT)
    if text.count(":") > 1:
        if is_ip(text):
            return text, 53
        raise SettingError(_FORMAT)
    if ":" in text:
        host, port_s = text.rsplit(":", 1)
        if not port_s.isdigit():
            raise SettingError(_FORMAT)
        return host, _port(port_s)
    return text, 53


def _port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise SettingError("DNS port must be between 1 and 65535.")
    return port


def _format_ip(host: str, port: int, prefix: str) -> str:
    shown = f"[{host}]" if ":" in host else host
    if port == 53:
        return f"{prefix}{shown}"
    return f"{prefix}{shown}:{port}"


def _short(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return "timeout"
    text = str(exc).strip() or exc.__class__.__name__
    return text[:180]


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
