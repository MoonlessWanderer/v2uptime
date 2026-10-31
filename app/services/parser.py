"""Parse V2Ray share links (vless / vmess / trojan / ss) into Xray outbounds.

Supported transports: tcp (+http header), ws, grpc, httpupgrade, xhttp, kcp.
Supported security: none, tls, reality.

Every public function raises :class:`LinkParseError` with a Persian message
suitable for the admin panel when the input cannot be used.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

SUPPORTED_PROTOCOLS = ("vless", "vmess", "trojan", "ss")
SUPPORTED_NETWORKS = ("tcp", "ws", "grpc", "httpupgrade", "xhttp", "kcp")
SUPPORTED_SECURITY = ("none", "tls", "reality")

_NETWORK_ALIASES = {"": "tcp", "raw": "tcp", "splithttp": "xhttp", "mkcp": "kcp", "gun": "grpc", "websocket": "ws"}
_REMOVED_NETWORKS = {"h2": "HTTP/2", "http": "HTTP/2", "quic": "QUIC"}
_KNOWN_UNSUPPORTED = {"hysteria", "hysteria2", "hy2", "tuic", "wireguard", "wg", "socks", "http", "https", "ssr"}
_VLESS_FLOWS = ("", "xtls-rprx-vision", "xtls-rprx-vision-udp443")
_VMESS_CIPHERS = ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero")
_SS_METHODS = {
    "aes-128-gcm": "aes-128-gcm",
    "aes-256-gcm": "aes-256-gcm",
    "chacha20-poly1305": "chacha20-poly1305",
    "chacha20-ietf-poly1305": "chacha20-poly1305",
    "xchacha20-poly1305": "xchacha20-poly1305",
    "xchacha20-ietf-poly1305": "xchacha20-poly1305",
    "none": "none",
    "plain": "none",
    "2022-blake3-aes-128-gcm": "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm": "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305": "2022-blake3-chacha20-poly1305",
}
_FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized",
                 "randomizednoalpn", "unsafe")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_\-]{0,62})(?:\.[A-Za-z0-9_\-]{1,63})*\.?$")
_MAX_LINK_LENGTH = 8192


class LinkParseError(ValueError):
    """Raised for an unusable share link; the message is shown to the admin."""


@dataclass
class ParsedLink:
    protocol: str  # vless / vmess / trojan / ss
    address: str
    port: int
    remark: str
    network: str
    security: str
    credential: str  # uuid or password (for identity only)
    outbound: dict[str, Any]  # Xray outbound object without "tag"
    raw: str
    country: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        """Stable key: same server even if transport params or remark change."""
        key = f"{self.protocol}|{self.address.lower()}|{self.port}|{self.credential}"
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    @property
    def default_name(self) -> str:
        if self.remark:
            return self.remark[:200]
        # Never derive a public name from the address.
        return f"{self.protocol.upper()}-{self.identity[:6]}"


# ---------------------------------------------------------------- helpers

def b64decode_text(data: str) -> str:
    """Decode standard or URL-safe base64, with or without padding."""
    cleaned = re.sub(r"\s+", "", data).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise LinkParseError("رشته Base64 نامعتبر است.") from None


def _parse_query(query: str) -> dict[str, str]:
    """Like parse_qs but keeps '+' literally (share links use %20 for spaces)."""
    params: dict[str, str] = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = unquote(key).strip()
        if key and key not in params:
            params[key] = unquote(value).strip()
    return params


def _validate_host(host: str) -> str:
    host = (host or "").strip().strip("[]")
    if not host:
        raise LinkParseError("آدرس سرور در لینک وجود ندارد.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOST_RE.match(host):
        raise LinkParseError(f"آدرس سرور نامعتبر است: {host}")
    return host.lower().rstrip(".")


def _validate_port(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise LinkParseError("پورت سرور نامعتبر است یا وجود ندارد.") from None
    if not 1 <= port <= 65535:
        raise LinkParseError(f"پورت باید بین ۱ تا ۶۵۵۳۵ باشد (مقدار فعلی: {port}).")
    return port


def _split_netloc(netloc: str) -> tuple[str, int]:
    """Parse 'host:port' (IPv6 in brackets) into a validated pair."""
    try:
        parts = urlsplit("//" + netloc)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise LinkParseError("پورت سرور نامعتبر است.") from None
    if port is None:
        raise LinkParseError("پورت سرور در لینک مشخص نشده است.")
    return _validate_host(host or ""), _validate_port(port)


def _validate_uuid(value: str, protocol: str) -> str:
    value = (value or "").strip()
    if not value:
        raise LinkParseError(f"شناسه کاربر (UUID) در لینک {protocol} وجود ندارد.")
    # Xray also accepts any 1-30 byte string and maps it to a UUIDv5.
    if not _UUID_RE.match(value) and not (len(value.encode()) <= 30 and not re.search(r"\s", value)):
        raise LinkParseError(f"شناسه کاربر (UUID) نامعتبر است: {value[:40]}")
    return value


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _normalize_network(value: str) -> str:
    net = (value or "").strip().lower()
    net = _NETWORK_ALIASES.get(net, net)
    if net in _REMOVED_NETWORKS:
        raise LinkParseError(f"ترنسپورت {_REMOVED_NETWORKS[net]} در نسخه‌های جدید Xray حذف شده است.")
    if net not in SUPPORTED_NETWORKS:
        raise LinkParseError(f"ترنسپورت پشتیبانی نمی‌شود: {value}")
    return net


def _normalize_security(value: str, default: str) -> str:
    sec = (value or "").strip().lower() or default
    if sec == "xtls":
        raise LinkParseError("امنیت XTLS قدیمی است؛ از tls یا reality همراه با flow=xtls-rprx-vision استفاده کنید.")
    if sec not in SUPPORTED_SECURITY:
        raise LinkParseError(f"نوع امنیت پشتیبانی نمی‌شود: {value}")
    return sec


def _csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def country_from_remark(remark: str) -> str:
    """Extract an ISO country code from a flag emoji in the remark, if any."""
    chars = [c for c in remark if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    if len(chars) >= 2:
        return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in chars[:2])
    return ""


def flag_emoji(country: str) -> str:
    country = (country or "").upper()
    if len(country) != 2 or not country.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country)


# ------------------------------------------------------ stream settings

def build_stream_settings(network: str, security: str, p: dict[str, str], address: str) -> dict[str, Any]:
    """Build Xray ``streamSettings`` from share-link style parameters.

    ``p`` uses share-link keys: host, path, headerType, serviceName, mode,
    authority, seed, extra, sni, alpn, fp, allowInsecure, pbk, sid, spx, pqv.
    """
    stream: dict[str, Any] = {"network": network, "security": security}
    host = p.get("host", "")
    path = p.get("path", "")

    if network == "tcp":
        header_type = (p.get("headerType") or "none").lower()
        if header_type == "http":
            request: dict[str, Any] = {"path": _csv(path) or ["/"]}
            if host:
                request["headers"] = {"Host": _csv(host)}
            stream["tcpSettings"] = {"header": {"type": "http", "request": request}}
        elif header_type not in ("none", ""):
            raise LinkParseError(f"headerType نامعتبر برای tcp: {header_type}")
    elif network == "ws":
        ws: dict[str, Any] = {"path": path or "/"}
        if host:
            ws["host"] = host
        stream["wsSettings"] = ws
    elif network == "grpc":
        grpc: dict[str, Any] = {
            "serviceName": p.get("serviceName") or path or "",
            "multiMode": (p.get("mode") or "").lower() == "multi",
        }
        if p.get("authority"):
            grpc["authority"] = p["authority"]
        stream["grpcSettings"] = grpc
    elif network == "httpupgrade":
        hu: dict[str, Any] = {"path": path or "/"}
        if host:
            hu["host"] = host
        stream["httpupgradeSettings"] = hu
    elif network == "xhttp":
        mode = (p.get("mode") or "auto").lower()
        if mode not in ("auto", "packet-up", "stream-up", "stream-one"):
            raise LinkParseError(f"mode نامعتبر برای xhttp: {mode}")
        xh: dict[str, Any] = {"path": path or "/", "mode": mode}
        if host:
            xh["host"] = host
        if p.get("extra"):
            try:
                extra = json.loads(p["extra"])
            except json.JSONDecodeError:
                raise LinkParseError("پارامتر extra در xhttp باید JSON معتبر باشد.") from None
            if not isinstance(extra, dict):
                raise LinkParseError("پارامتر extra در xhttp باید یک شیء JSON باشد.")
            xh["extra"] = extra
        stream["xhttpSettings"] = xh
    elif network == "kcp":
        kcp: dict[str, Any] = {"header": {"type": (p.get("headerType") or "none").lower()}}
        if p.get("seed"):
            kcp["seed"] = p["seed"]
        stream["kcpSettings"] = kcp

    fp = (p.get("fp") or "").lower()
    if fp and fp not in _FINGERPRINTS:
        raise LinkParseError(f"fingerprint نامعتبر است: {fp}")

    if security == "tls":
        sni = p.get("sni") or p.get("peer") or host or ("" if _is_ip(address) else address)
        tls: dict[str, Any] = {}
        if sni:
            tls["serverName"] = sni
        if p.get("alpn"):
            tls["alpn"] = _csv(p["alpn"])
        if fp:
            tls["fingerprint"] = fp
        if (p.get("allowInsecure") or p.get("insecure") or "").lower() in ("1", "true"):
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    elif security == "reality":
        pbk = p.get("pbk", "")
        if not pbk:
            raise LinkParseError("پارامتر pbk (کلید عمومی) برای reality الزامی است.")
        if not re.match(r"^[A-Za-z0-9_\-]{40,50}$", pbk):
            raise LinkParseError("کلید عمومی reality (pbk) نامعتبر است.")
        sni = p.get("sni") or p.get("peer") or ""
        if not sni:
            raise LinkParseError("پارامتر sni برای reality الزامی است.")
        sid = p.get("sid", "")
        if sid and not re.match(r"^[0-9a-fA-F]{0,16}$", sid):
            raise LinkParseError("shortId (sid) باید هگزادسیمال و حداکثر ۱۶ کاراکتر باشد.")
        reality: dict[str, Any] = {
            "serverName": sni,
            "fingerprint": fp or "chrome",
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": p.get("spx") or "/",
        }
        if p.get("pqv"):
            reality["mldsa65Verify"] = p["pqv"]
        stream["realitySettings"] = reality
    return stream


# ------------------------------------------------------------- protocols

def _split_link(link: str) -> tuple[str, str, str]:
    """Return (body, query, remark) for scheme://body?query#remark."""
    rest = link.split("://", 1)[1]
    rest, _, fragment = rest.partition("#")
    body, _, query = rest.partition("?")
    return body.rstrip("/"), query, unquote(fragment).strip()


def _parse_url_style(link: str, protocol: str) -> tuple[str, str, int, dict[str, str], str]:
    """Parse protocol://user@host:port?query#remark."""
    body, query, remark = _split_link(link)
    if "@" not in body:
        raise LinkParseError(f"ساختار لینک {protocol} نامعتبر است (بخش user@host:port پیدا نشد).")
    user, _, netloc = body.rpartition("@")
    host, port = _split_netloc(netloc)
    return unquote(user), host, port, _parse_query(query), remark


def _parse_vless(link: str) -> ParsedLink:
    uid, host, port, p, remark = _parse_url_style(link, "vless")
    uid = _validate_uuid(uid, "vless")
    network = _normalize_network(p.get("type", ""))
    security = _normalize_security(p.get("security", ""), "none")
    flow = p.get("flow", "")
    if flow not in _VLESS_FLOWS:
        raise LinkParseError(f"flow نامعتبر است: {flow}")
    user: dict[str, Any] = {"id": uid, "encryption": p.get("encryption") or "none"}
    if flow:
        user["flow"] = flow
    outbound = {
        "protocol": "vless",
        "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
        "streamSettings": build_stream_settings(network, security, p, host),
    }
    return ParsedLink("vless", host, port, remark, network, security, uid, outbound, link)


def _vmess_outbound(host: str, port: int, uid: str, cipher: str, network: str, security: str,
                    p: dict[str, str]) -> dict[str, Any]:
    cipher = (cipher or "auto").lower()
    if cipher not in _VMESS_CIPHERS:
        raise LinkParseError(f"رمزنگاری vmess پشتیبانی نمی‌شود: {cipher}")
    return {
        "protocol": "vmess",
        "settings": {"vnext": [{"address": host, "port": port, "users": [{"id": uid, "security": cipher}]}]},
        "streamSettings": build_stream_settings(network, security, p, host),
    }


def _parse_vmess(link: str) -> ParsedLink:
    payload = link.split("://", 1)[1].strip()
    body = payload.split("#", 1)[0].split("?", 1)[0]
    if "@" in body:  # Xray's URL-style vmess (VMessAEAD) share link
        uid, host, port, p, remark = _parse_url_style(link, "vmess")
        uid = _validate_uuid(uid, "vmess")
        network = _normalize_network(p.get("type", ""))
        security = _normalize_security(p.get("security", ""), "none")
        outbound = _vmess_outbound(host, port, uid, p.get("encryption", "auto"), network, security, p)
        return ParsedLink("vmess", host, port, remark, network, security, uid, outbound, link)

    try:
        data = json.loads(b64decode_text(payload.split("#", 1)[0]))
    except (LinkParseError, json.JSONDecodeError):
        raise LinkParseError("لینک vmess نامعتبر است (Base64 یا JSON داخل آن قابل خواندن نیست).") from None
    if not isinstance(data, dict):
        raise LinkParseError("لینک vmess نامعتبر است (JSON باید یک شیء باشد).")

    def val(key: str) -> str:
        value = data.get(key)
        return "" if value is None else str(value).strip()

    host = _validate_host(val("add"))
    port = _validate_port(val("port"))
    uid = _validate_uuid(val("id"), "vmess")
    network = _normalize_network(val("net"))
    security = _normalize_security(val("tls"), "none")
    header = val("type")
    p: dict[str, str] = {
        "host": val("host"), "path": val("path"), "sni": val("sni"), "alpn": val("alpn"),
        "fp": val("fp"), "allowInsecure": val("allowInsecure") or val("insecure"),
        "pbk": val("pbk"), "sid": val("sid"), "spx": val("spx"),
    }
    if network in ("tcp", "kcp"):
        p["headerType"] = header
    if network == "kcp":
        p["seed"] = val("path")
    if network == "grpc":
        p["serviceName"] = val("path")
        p["mode"] = "multi" if header == "multi" else "gun"
        p["authority"] = val("authority")
    if network == "xhttp":
        p["mode"] = val("mode") or (header if header not in ("", "none") else "auto")
        if val("extra"):
            p["extra"] = val("extra")
    outbound = _vmess_outbound(host, port, uid, val("scy"), network, security, p)
    return ParsedLink("vmess", host, port, val("ps"), network, security, uid, outbound, link)


def _parse_trojan(link: str) -> ParsedLink:
    password, host, port, p, remark = _parse_url_style(link, "trojan")
    if not password:
        raise LinkParseError("رمز عبور در لینک trojan وجود ندارد.")
    network = _normalize_network(p.get("type", ""))
    security = _normalize_security(p.get("security", ""), "tls")
    outbound = {
        "protocol": "trojan",
        "settings": {"servers": [{"address": host, "port": port, "password": password}]},
        "streamSettings": build_stream_settings(network, security, p, host),
    }
    return ParsedLink("trojan", host, port, remark, network, security, password, outbound, link)


def _ss_userinfo(userinfo: str) -> tuple[str, str]:
    """SIP002 userinfo: base64url(method:password) or percent-encoded plain text."""
    decoded = unquote(userinfo)
    method, sep, password = decoded.partition(":")
    if not sep or method.lower() not in _SS_METHODS:
        method, sep, password = b64decode_text(decoded).partition(":")
    if not sep:
        raise LinkParseError("بخش method:password در لینک ss نامعتبر است.")
    return method, password


def _parse_ss(link: str) -> ParsedLink:
    body, query, remark = _split_link(link)
    p = _parse_query(query)
    if p.get("plugin"):
        raise LinkParseError("پلاگین‌های Shadowsocks (مثل obfs یا v2ray-plugin) در Xray پشتیبانی نمی‌شوند.")
    if "@" in body:  # SIP002
        userinfo, _, netloc = body.rpartition("@")
        method, password = _ss_userinfo(userinfo)
    else:  # legacy: base64(method:password@host:port)
        decoded = b64decode_text(body)
        creds, sep, netloc = decoded.rpartition("@")
        if not sep:
            raise LinkParseError("لینک ss نامعتبر است.")
        method, _, password = creds.partition(":")
    host, port = _split_netloc(netloc)
    method_key = method.strip().lower()
    if method_key not in _SS_METHODS:
        raise LinkParseError(f"روش رمزنگاری Shadowsocks در Xray پشتیبانی نمی‌شود: {method}")
    if not password:
        raise LinkParseError("رمز عبور در لینک ss وجود ندارد.")
    network = _normalize_network(p.get("type", ""))
    security = _normalize_security(p.get("security", ""), "none")
    server: dict[str, Any] = {"address": host, "port": port, "method": _SS_METHODS[method_key], "password": password}
    outbound = {
        "protocol": "shadowsocks",
        "settings": {"servers": [server]},
        "streamSettings": build_stream_settings(network, security, p, host),
    }
    return ParsedLink("ss", host, port, remark, network, security, password, outbound, link)


_PARSERS = {"vless": _parse_vless, "vmess": _parse_vmess, "trojan": _parse_trojan, "ss": _parse_ss}


def parse_link(link: str) -> ParsedLink:
    """Parse a single share link."""
    link = (link or "").strip()
    if not link:
        raise LinkParseError("لینک خالی است.")
    if len(link) > _MAX_LINK_LENGTH:
        raise LinkParseError("لینک بیش از حد طولانی است.")
    if "://" not in link:
        raise LinkParseError("لینک باید با پروتکل شروع شود (مثلاً vless://).")
    scheme = link.split("://", 1)[0].lower()
    parser = _PARSERS.get(scheme)
    if parser is None:
        if scheme in _KNOWN_UNSUPPORTED:
            raise LinkParseError(f"پروتکل {scheme} پشتیبانی نمی‌شود (فقط vless، vmess، trojan و ss).")
        raise LinkParseError(f"پروتکل ناشناخته: {scheme}")
    parsed = parser(link)
    parsed.remark = parsed.remark.strip()[:200]
    parsed.country = country_from_remark(parsed.remark)
    return parsed


def decode_subscription(content: str | bytes) -> str:
    """Return newline-separated links from subscription content (Base64 or plain)."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    text = content.strip().lstrip("﻿")
    if not text:
        raise LinkParseError("محتوای Subscription خالی است.")
    if "://" in text:
        return text
    try:
        decoded = b64decode_text(text)
    except LinkParseError:
        raise LinkParseError("محتوای Subscription نه Base64 معتبر است و نه لیست لینک.") from None
    if "://" not in decoded:
        raise LinkParseError("در محتوای Subscription هیچ لینکی پیدا نشد.")
    return decoded


@dataclass
class BulkResult:
    links: list[ParsedLink]
    errors: list[tuple[int, str, str]]  # (line number, line excerpt, message)


def parse_many(text: str) -> BulkResult:
    """Parse one link per line; a Base64 subscription blob is decoded first."""
    text = (text or "").strip()
    if text and "://" not in text:
        try:
            text = decode_subscription(text)
        except LinkParseError:
            pass
    links: list[ParsedLink] = []
    errors: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith(";"):
            continue
        try:
            links.append(parse_link(line))
        except LinkParseError as exc:
            excerpt = line if len(line) <= 60 else line[:57] + "..."
            errors.append((number, excerpt, str(exc)))
    return BulkResult(links, errors)
