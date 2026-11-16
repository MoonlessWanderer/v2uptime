"""Primary and secondary DNS: Xray config, validation, and failure labels."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Config
from app.services import dns
from app.services.dns import Resolution
from app.services.parser import parse_link
from app.services.settings import SettingError, settings
from app.services.xray_config import BLOCK_TAG, XrayEntry, build_config
from app.services.xray_manager import XrayManager

from tests.conftest import make_server
from tests.test_web import app_factory, client, csrf_from, login  # noqa: F401

FAKE = Path(__file__).with_name("fake_xray.py")
UUID = "d342d11e-d424-4583-b36e-524ab1f0afa4"


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    dns.clear_cache()
    yield
    dns.clear_cache()


def _domain_entry(sockopt=None):
    parsed = parse_link(f"vless://{UUID}@a.example.com:443?type=ws&security=tls#A")
    if sockopt:
        parsed.outbound.setdefault("streamSettings", {})["sockopt"] = dict(sockopt)
    return XrayEntry.create(1, 20000, parsed.outbound)


def _ip_entry():
    parsed = parse_link(f"vless://{UUID}@1.2.3.4:443?type=tcp#IP")
    return XrayEntry.create(2, 20001, parsed.outbound)


def test_empty_dns_matches_the_previous_config():
    config = build_config([_domain_entry(), _ip_entry()], api_port=10085)
    assert "dns" not in config
    assert config["routing"]["domainStrategy"] == "AsIs"
    assert [item["tag"] for item in config["outbounds"]] == [BLOCK_TAG, "out-1", "out-2"]
    assert "sockopt" not in json.dumps(config)


def test_dns_config_routes_queries_and_forces_ip_on_domains_only():
    config = build_config(
        [_domain_entry({"tcpFastOpen": True}), _ip_entry()],
        api_port=10085,
        dns_servers=["8.8.8.8", "tcp://1.1.1.1"],
    )
    assert config["outbounds"][0] == {"tag": BLOCK_TAG, "protocol": "blackhole"}
    assert config["outbounds"][-1] == {"tag": "direct", "protocol": "freedom"}
    assert config["dns"]["tag"] == "dns-query"
    assert "queryStrategy" not in config["dns"]
    assert config["dns"]["servers"][0] == {
        "address": "8.8.8.8", "timeoutMs": 3000, "unexpectedIPs": ["10.10.34.0/24"],
    }
    assert config["dns"]["servers"][1]["address"] == "tcp://1.1.1.1"
    assert config["dns"]["servers"][1]["unexpectedIPs"] == ["10.10.34.0/24"]
    assert config["routing"]["domainStrategy"] == "AsIs"
    assert config["routing"]["rules"][0] == {
        "type": "field", "inboundTag": ["dns-query"], "outboundTag": "direct",
    }
    sock = config["outbounds"][1]["streamSettings"]["sockopt"]
    assert sock == {"tcpFastOpen": True, "domainStrategy": "ForceIP"}
    assert "domainStrategy" not in json.dumps(config["outbounds"][2])


def test_ipv4_only_sets_query_strategy_and_force_ipv4():
    config = build_config([_domain_entry()], api_port=1, dns_servers=["localhost"], ipv4_only=True)
    assert config["dns"]["queryStrategy"] == "UseIPv4"
    assert config["dns"]["servers"][0]["address"] == "localhost"
    assert config["outbounds"][1]["streamSettings"]["sockopt"]["domainStrategy"] == "ForceIPv4"


def test_non_default_port_is_a_separate_field():
    server = dns.xray_server("8.8.8.8:5353")
    assert server["address"] == "8.8.8.8" and server["port"] == 5353


@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("  ", ""),
    ("8.8.8.8", "8.8.8.8"),
    ("8.8.8.8:53", "8.8.8.8"),
    ("8.8.8.8:5353", "8.8.8.8:5353"),
    ("tcp://1.1.1.1", "tcp://1.1.1.1"),
    ("tcp://1.1.1.1:53", "tcp://1.1.1.1"),
    ("tcp://1.1.1.1:5353", "tcp://1.1.1.1:5353"),
    ("https://1.1.1.1/dns-query", "https://1.1.1.1/dns-query"),
    ("https://1.1.1.1", "https://1.1.1.1/dns-query"),
    ("LocalHost", "localhost"),
])
def test_dns_formats_are_accepted(raw, expected):
    assert dns.parse_dns(raw) == expected


@pytest.mark.parametrize("raw, message", [
    ("dns.google", "8.8.8.8"),
    ("tcp://dns.google", "tcp://"),
    ("udp://8.8.8.8", "8.8.8.8"),
    ("8.8.8.8:99999", "65535"),
    ("not a dns", "8.8.8.8"),
])
def test_dns_formats_are_rejected(raw, message):
    with pytest.raises(SettingError, match=message):
        dns.parse_dns(raw)


def test_filtered_answer_falls_through_to_the_secondary(database, monkeypatch):
    def query(spec, name, ipv4):
        assert name == "cdn.example.com"
        if spec == "8.8.8.8":
            return ["10.10.34.5", "10.10.34.6"]
        return ["9.9.9.9"]

    monkeypatch.setattr(dns, "query_records", query)
    settings.set_many({"dns_primary": "8.8.8.8", "dns_secondary": "1.1.1.1"})
    answer = dns.resolve("cdn.example.com")
    assert answer.ips == ["9.9.9.9"] and answer.source == "secondary"
    assert dns.failure_kind("cdn.example.com") is None


def test_only_filtered_answers_are_dns_blocked(database, monkeypatch):
    monkeypatch.setattr(dns, "query_records", lambda spec, name, ipv4: ["10.10.34.8"])
    settings.set("dns_primary", "8.8.8.8")
    assert dns.failure_kind("cdn.example.com") == "dns_blocked"


def test_a_real_address_beside_a_filtered_one_is_kept(database, monkeypatch):
    monkeypatch.setattr(dns, "query_records", lambda spec, name, ipv4: ["10.10.34.1", "9.9.9.9"])
    settings.set("dns_primary", "8.8.8.8")
    answer = dns.resolve("cdn.example.com")
    assert answer.ips == ["9.9.9.9"] and answer.source == "primary"


def test_unreachable_dns_is_a_dns_error(database, monkeypatch):
    def query(spec, name, ipv4):
        raise OSError("network unreachable")

    monkeypatch.setattr(dns, "query_records", query)
    settings.set("dns_primary", "8.8.8.8")
    assert dns.failure_kind("cdn.example.com") == "dns"


def test_localhost_secondary_is_reported_as_system(database, monkeypatch):
    def query(spec, name, ipv4):
        if spec == "8.8.8.8":
            raise TimeoutError("timed out")
        assert spec == "localhost"
        return ["1.1.1.1"]

    monkeypatch.setattr(dns, "query_records", query)
    settings.set_many({"dns_primary": "8.8.8.8", "dns_secondary": "localhost"})
    answer = dns.resolve("cdn.example.com")
    assert answer.source == "system" and answer.ips == ["1.1.1.1"]


def test_resolution_is_cached_for_the_same_chain(database, monkeypatch):
    calls = []

    def query(spec, name, ipv4):
        calls.append(name)
        return ["1.1.1.1"]

    monkeypatch.setattr(dns, "query_records", query)
    settings.set("dns_primary", "8.8.8.8")
    dns.resolve("cdn.example.com")
    dns.resolve("cdn.example.com")
    assert calls == ["cdn.example.com"]


def test_fake_xray_accepts_a_dns_section(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(build_config([], 10085, dns_servers=["8.8.8.8"])), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(FAKE), "run", "-test", "-c", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _manager(tmp_path):
    cfg = Config(db_path=tmp_path / "uptime.db", xray_bin=FAKE)
    manager = XrayManager(cfg)
    manager.argv = lambda *args: [sys.executable, str(FAKE), *args]
    return manager


def test_installed_xray_must_accept_unexpected_ips(tmp_path):
    ok, message = asyncio.run(_manager(tmp_path).accepts_dns_config())
    assert ok and message == ""


def test_old_xray_core_refuses_the_dns_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_XRAY_REJECT_DNS", "1")
    ok, message = asyncio.run(_manager(tmp_path).accepts_dns_config())
    assert ok is False
    assert "25.6.8" in message


def test_settings_page_has_the_dns_group(client):
    login(client)
    text = client.get("/admin/settings").text
    assert "DNS for config domains" in text
    assert "as the secondary to fall back to the server" in text
    assert 'id="dns-test"' in text


def test_invalid_dns_is_not_saved(client):
    login(client)
    token = csrf_from(client.get("/admin/settings").text)
    response = client.post("/admin/settings", data={"csrf_token": token, "dns_primary": "dns.google"},
                           follow_redirects=True)
    assert "8.8.8.8" in response.text
    assert settings.get("dns_primary") == ""


def test_rejected_dns_config_is_not_saved(client, monkeypatch):
    login(client)

    async def reject(self):
        return False, (
            "This Xray core does not support DNS filtering (unexpectedIPs). "
            "Update the Xray core (25.6.8 or newer) and try again."
        )

    monkeypatch.setattr(XrayManager, "accepts_dns_config", reject)
    token = csrf_from(client.get("/admin/settings").text)
    response = client.post("/admin/settings", data={"csrf_token": token, "dns_primary": "8.8.8.8"},
                           follow_redirects=True)
    assert "Update the Xray core" in response.text
    assert settings.get("dns_primary") == ""


def test_accepted_dns_is_saved_and_restarts_xray(client, monkeypatch):
    login(client)
    reasons = []

    async def accept(self):
        return True, ""

    async def restart(self, reason):
        reasons.append(reason)
        return True

    monkeypatch.setattr(XrayManager, "accepts_dns_config", accept)
    monkeypatch.setattr(XrayManager, "restart", restart)
    monkeypatch.setattr(XrayManager, "installed", property(lambda self: True))
    token = csrf_from(client.get("/admin/settings").text)
    response = client.post(
        "/admin/settings",
        data={"csrf_token": token, "dns_primary": "8.8.8.8", "dns_secondary": "localhost", "dns_ipv4_only": "1"},
        follow_redirects=True,
    )
    assert "Settings saved." in response.text
    assert settings.get("dns_primary") == "8.8.8.8"
    assert settings.get("dns_secondary") == "localhost"
    assert settings.get("dns_ipv4_only") is True
    assert reasons == ["dns settings"]


def test_dns_test_reports_each_server(client, monkeypatch):
    login(client)

    def query(spec, name, ipv4):
        assert name == "www.google.com"
        if spec == "8.8.8.8":
            return ["1.2.3.4"]
        raise TimeoutError("timed out")

    monkeypatch.setattr(dns, "query_records", query)
    token = csrf_from(client.get("/admin/settings").text)
    response = client.post(
        "/admin/settings/dns-test",
        json={"dns_primary": "8.8.8.8", "dns_secondary": "1.1.1.1"},
        headers={"X-CSRF-Token": token},
    )
    body = response.json()
    assert body["results"][0]["ok"] and body["results"][0]["ips"] == ["1.2.3.4"]
    assert body["results"][1]["ok"] is False and body["results"][1]["error"] == "timeout"


def test_dns_test_rejects_a_bad_address(client):
    login(client)
    token = csrf_from(client.get("/admin/settings").text)
    response = client.post(
        "/admin/settings/dns-test",
        json={"dns_primary": "nope"},
        headers={"X-CSRF-Token": token},
    )
    assert "8.8.8.8" in response.json()["error"]


def test_server_detail_shows_dns_for_a_domain(client, monkeypatch):
    login(client)
    server = make_server("Alpha", address="cdn.example.com")
    monkeypatch.setattr(
        "app.routers.admin.dnsdiag.resolve",
        lambda name: Resolution(["9.9.9.9"], "primary", 1_700_000_000),
    )
    text = client.get(f"/admin/servers/{server.id}").text
    assert "9.9.9.9" in text and "primary" in text

    monkeypatch.setattr(
        "app.routers.admin.dnsdiag.resolve",
        lambda name: Resolution([], "secondary", 1_700_000_000, error="filtered answer", blocked=True),
    )
    blocked = client.get(f"/admin/servers/{server.id}").text
    assert "DNS blocked (filtered answer)" in blocked and "secondary" in blocked


def test_server_detail_skips_dns_for_an_ip(client, monkeypatch):
    login(client)
    server = make_server("Alpha", address="1.2.3.4")
    called = []
    monkeypatch.setattr("app.routers.admin.dnsdiag.resolve", lambda name: called.append(name))
    text = client.get(f"/admin/servers/{server.id}").text
    assert called == []
    assert "<dt>DNS</dt>" not in text
