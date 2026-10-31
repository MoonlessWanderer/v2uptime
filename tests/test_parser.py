import base64
import json
from urllib.parse import quote

import pytest

from app.services.parser import (
    LinkParseError,
    country_from_remark,
    decode_subscription,
    parse_link,
    parse_many,
)

UUID = "d342d11e-d424-4583-b36e-524ab1f0afa4"
PBK = "SbVKOEMjK0sIlbwg4akyBg5mL5KZwwB-ed4eEE7YnRc"


def b64(text: str, urlsafe: bool = False) -> str:
    raw = text.encode()
    return (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode()


# ------------------------------------------------------------------ vless

def test_vless_reality_vision():
    link = (
        f"vless://{UUID}@1.2.3.4:443?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni=www.microsoft.com&fp=chrome&pbk={PBK}&sid=6ba85179e30d4fc2&spx=%2F&type=tcp&headerType=none"
        "#%F0%9F%87%A9%F0%9F%87%AA%20Germany-Reality"
    )
    p = parse_link(link)
    assert (p.protocol, p.address, p.port, p.network, p.security) == ("vless", "1.2.3.4", 443, "tcp", "reality")
    assert p.remark == "🇩🇪 Germany-Reality"
    assert p.country == "DE"
    user = p.outbound["settings"]["vnext"][0]["users"][0]
    assert user == {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}
    reality = p.outbound["streamSettings"]["realitySettings"]
    assert reality == {
        "serverName": "www.microsoft.com", "fingerprint": "chrome", "publicKey": PBK,
        "shortId": "6ba85179e30d4fc2", "spiderX": "/",
    }


def test_vless_ws_tls():
    link = (
        f"vless://{UUID}@example.com:443?type=ws&security=tls&path=%2Fws%3Fed%3D2048"
        "&host=cdn.example.com&sni=cdn.example.com&alpn=h2%2Chttp%2F1.1&fp=firefox#WS"
    )
    p = parse_link(link)
    stream = p.outbound["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["wsSettings"] == {"path": "/ws?ed=2048", "host": "cdn.example.com"}
    assert stream["tlsSettings"] == {"serverName": "cdn.example.com", "alpn": ["h2", "http/1.1"], "fingerprint": "firefox"}


def test_vless_tls_sni_defaults_to_address_and_ipv6():
    p = parse_link(f"vless://{UUID}@my.host.net:8443?security=tls#x")
    assert p.outbound["streamSettings"]["tlsSettings"]["serverName"] == "my.host.net"
    p6 = parse_link(f"vless://{UUID}@[2001:db8::1]:443?security=tls#v6")
    assert p6.address == "2001:db8::1"
    assert "serverName" not in p6.outbound["streamSettings"]["tlsSettings"]


def test_vless_xhttp_with_extra():
    extra = "%7B%22xPaddingBytes%22%3A%22100-1000%22%7D"
    p = parse_link(f"vless://{UUID}@x.example.com:443?type=xhttp&path=%2Fxh&mode=packet-up&security=tls"
                   f"&sni=x.example.com&extra={extra}#XHTTP")
    xh = p.outbound["streamSettings"]["xhttpSettings"]
    assert xh == {"path": "/xh", "mode": "packet-up", "extra": {"xPaddingBytes": "100-1000"}}


def test_vless_httpupgrade_and_kcp_and_grpc():
    hu = parse_link(f"vless://{UUID}@h.example.com:80?type=httpupgrade&path=%2Fhu&host=h.example.com#HU")
    assert hu.outbound["streamSettings"]["httpupgradeSettings"] == {"path": "/hu", "host": "h.example.com"}
    assert hu.security == "none"

    kcp = parse_link(f"vless://{UUID}@k.example.com:1234?type=kcp&headerType=wechat-video&seed=s3#KCP")
    assert kcp.outbound["streamSettings"]["kcpSettings"] == {"header": {"type": "wechat-video"}, "seed": "s3"}

    grpc = parse_link(f"vless://{UUID}@g.example.com:443?type=grpc&serviceName=svc&mode=multi&security=tls#G")
    assert grpc.outbound["streamSettings"]["grpcSettings"] == {"serviceName": "svc", "multiMode": True}


def test_vless_tcp_http_header():
    p = parse_link(f"vless://{UUID}@t.example.com:80?type=tcp&headerType=http&host=a.com,b.com&path=%2Fp#T")
    header = p.outbound["streamSettings"]["tcpSettings"]["header"]
    assert header == {"type": "http", "request": {"path": ["/p"], "headers": {"Host": ["a.com", "b.com"]}}}


def test_raw_alias_maps_to_tcp():
    p = parse_link(f"vless://{UUID}@t.example.com:80?type=raw#T")
    assert p.network == "tcp"


# ------------------------------------------------------------------ vmess

def vmess_link(**overrides) -> str:
    data = {"v": "2", "ps": "vmess-ws", "add": "vm.example.com", "port": "8443", "id": UUID, "aid": "0",
            "scy": "auto", "net": "ws", "type": "none", "host": "vm.example.com", "path": "/vm", "tls": "tls",
            "sni": "vm.example.com", "alpn": "", "fp": "chrome"}
    data.update(overrides)
    return "vmess://" + b64(json.dumps(data))


def test_vmess_json_ws_tls():
    p = parse_link(vmess_link())
    assert (p.protocol, p.address, p.port, p.remark, p.network, p.security) == (
        "vmess", "vm.example.com", 8443, "vmess-ws", "ws", "tls")
    user = p.outbound["settings"]["vnext"][0]["users"][0]
    assert user == {"id": UUID, "security": "auto"}
    assert p.outbound["streamSettings"]["wsSettings"] == {"path": "/vm", "host": "vm.example.com"}


def test_vmess_json_tcp_http_and_grpc():
    tcp = parse_link(vmess_link(net="tcp", type="http", path="/a", tls=""))
    assert tcp.outbound["streamSettings"]["tcpSettings"]["header"]["type"] == "http"
    assert tcp.security == "none"
    grpc = parse_link(vmess_link(net="grpc", path="mysvc", type="multi"))
    assert grpc.outbound["streamSettings"]["grpcSettings"]["serviceName"] == "mysvc"
    assert grpc.outbound["streamSettings"]["grpcSettings"]["multiMode"] is True


def test_vmess_url_safe_base64_without_padding():
    data = json.dumps({"add": "1.1.1.1", "port": 443, "id": UUID, "net": "tcp", "ps": "x"})
    link = "vmess://" + b64(data, urlsafe=True).rstrip("=")
    assert parse_link(link).port == 443


def test_vmess_url_style():
    p = parse_link(f"vmess://{UUID}@v.example.com:443?type=ws&security=tls&path=%2Fw&encryption=auto#URL")
    assert p.network == "ws" and p.remark == "URL"


def test_vmess_invalid_base64():
    with pytest.raises(LinkParseError, match="vmess"):
        parse_link("vmess://not-base64!!!")


# ----------------------------------------------------------------- trojan

def test_trojan_grpc_defaults_to_tls():
    p = parse_link("trojan://p%40ss@tr.example.com:443?type=grpc&serviceName=grpcsvc&mode=multi"
                   "&sni=tr.example.com#Trojan%20gRPC")
    assert p.security == "tls"
    assert p.outbound["settings"]["servers"][0] == {"address": "tr.example.com", "port": 443, "password": "p@ss"}
    assert p.outbound["streamSettings"]["grpcSettings"]["serviceName"] == "grpcsvc"
    assert p.remark == "Trojan gRPC"


def test_trojan_missing_password():
    with pytest.raises(LinkParseError):
        parse_link("trojan://@tr.example.com:443#x")


# --------------------------------------------------------------------- ss

def test_ss_sip002_base64_userinfo():
    p = parse_link(f"ss://{b64('chacha20-ietf-poly1305:secret', urlsafe=True)}@5.6.7.8:8388#SS")
    server = p.outbound["settings"]["servers"][0]
    assert server == {"address": "5.6.7.8", "port": 8388, "method": "chacha20-poly1305", "password": "secret"}
    assert p.outbound["protocol"] == "shadowsocks"


def test_ss_2022_plain_userinfo():
    p = parse_link("ss://2022-blake3-aes-128-gcm:YctPZ6U7xPPcU%2Bgp3u%2BVHA%3D%3D@1.2.3.4:8388#SS2022")
    server = p.outbound["settings"]["servers"][0]
    assert server["method"] == "2022-blake3-aes-128-gcm"
    assert server["password"] == "YctPZ6U7xPPcU+gp3u+VHA=="


def test_ss_legacy_format():
    p = parse_link(f"ss://{b64('aes-256-gcm:pass@5.6.7.8:8388')}#Legacy")
    assert (p.address, p.port, p.remark) == ("5.6.7.8", 8388, "Legacy")


def test_ss_rejects_plugin_and_stream_cipher():
    with pytest.raises(LinkParseError, match="plugin"):
        parse_link(f"ss://{b64('aes-256-gcm:pass')}@1.2.3.4:8388/?plugin=obfs-local%3Bobfs%3Dhttp#x")
    with pytest.raises(LinkParseError, match="Unsupported Shadowsocks method"):
        parse_link(f"ss://{b64('aes-256-cfb:pass')}@1.2.3.4:8388#x")


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize(
    "link, fragment",
    [
        ("", "empty"),
        ("http://example.com", 'unsupported protocol "http"'),
        ("hysteria2://pw@1.2.3.4:443", 'unsupported protocol "hysteria2"'),
        ("foo://bar", "Unknown protocol"),
        ("just text", "protocol"),
        (f"vless://@host.com:443", "user id"),
        (f"vless://{UUID}@host.com", "port"),
        (f"vless://{UUID}@host.com:99999", "port"),
        (f"vless://{UUID}@host.com:443?type=h2", "removed"),
        (f"vless://{UUID}@host.com:443?type=quic", "removed"),
        (f"vless://{UUID}@host.com:443?security=reality&sni=a.com", "pbk"),
        (f"vless://{UUID}@host.com:443?security=reality&pbk={PBK}", "sni"),
        (f"vless://{UUID}@host.com:443?flow=bad-flow", "flow"),
        (f"vless://{UUID}@host.com:443?security=xtls", "XTLS"),
        (f"vless://{UUID}@host.com:443?type=xhttp&extra=notjson", "extra"),
        (f"vless://{UUID}@bad host:443", ""),
    ],
)
def test_invalid_links(link, fragment):
    with pytest.raises(LinkParseError) as info:
        parse_link(link)
    assert fragment in str(info.value)


# ------------------------------------------------------------------- bulk

def test_parse_many_reports_line_numbers():
    text = "\n".join([
        f"vless://{UUID}@a.com:443#A",
        "",
        "garbage line",
        f"trojan://pw@b.com:443#B",
    ])
    result = parse_many(text)
    assert [p.remark for p in result.links] == ["A", "B"]
    assert len(result.errors) == 1 and result.errors[0][0] == 3


def test_parse_many_accepts_base64_subscription():
    links = f"vless://{UUID}@a.com:443#A\ntrojan://pw@b.com:443#B"
    result = parse_many(b64(links))
    assert len(result.links) == 2 and not result.errors


def test_decode_subscription():
    assert "vless://" in decode_subscription(b64(f"vless://{UUID}@a.com:443#A").encode())
    with pytest.raises(LinkParseError):
        decode_subscription("")
    with pytest.raises(LinkParseError):
        decode_subscription(b64("hello world"))


def test_identity_ignores_remark_and_transport():
    a = parse_link(f"vless://{UUID}@a.com:443?type=ws&path=%2Fa#One")
    b = parse_link(f"vless://{UUID}@A.com:443?type=ws&path=%2Fb#Two")
    c = parse_link(f"vless://{UUID}@a.com:444#One")
    assert a.identity == b.identity != c.identity


def test_default_name_never_leaks_address():
    p = parse_link(f"vless://{UUID}@secret.example.com:443")
    assert "secret" not in p.default_name and p.default_name.startswith("VLESS-")


@pytest.mark.parametrize("remark, country", [
    ("🇩🇪 Germany | Reality", "DE"),
    ("Germany 🇩🇪", "DE"),
    ("🇩🇪", "DE"),
    ("🇮🇷➡️🇩🇪 Tunnel", "DE"),  # tunnel: the last flag is the exit country
    ("🇩 broken 🇪", ""),  # lone letters are not a flag
    ("🇩🇪🇫", "DE"),  # unfinished pair ignored
    ("Plain name", ""),
    ("آلمان 🇩🇪 ویژه", "DE"),
    ("🇳🇱 Amsterdam", "NL"),
])
def test_country_from_remark(remark, country):
    assert country_from_remark(remark) == country
    parsed = parse_link(f"vless://{UUID}@h.example.com:443?type=tcp#{quote(remark)}")
    assert parsed.default_name == remark and parsed.country == country
