from app.services.parser import parse_link
from app.services.xray_config import (
    BLOCK_TAG,
    XrayEntry,
    api_inbound_payload,
    api_outbound_payload,
    api_rule_payload,
    build_config,
    build_test_config,
)

UUID = "d342d11e-d424-4583-b36e-524ab1f0afa4"


def entries():
    a = parse_link(f"vless://{UUID}@a.example.com:443?type=ws&security=tls#A")
    b = parse_link("trojan://pw@b.example.com:443#B")
    return [XrayEntry.create(1, 20000, a.outbound), XrayEntry.create(2, 20001, b.outbound)]


def test_full_config_layout():
    config = build_config(entries(), api_port=10085)
    assert config["api"]["listen"] == "127.0.0.1:10085"
    assert set(config["api"]["services"]) == {"HandlerService", "RoutingService"}
    # Blackhole first: unmatched traffic must never go out directly.
    assert config["outbounds"][0] == {"tag": BLOCK_TAG, "protocol": "blackhole"}
    assert [o["tag"] for o in config["outbounds"][1:]] == ["out-1", "out-2"]
    inbound = config["inbounds"][0]
    assert inbound == {"tag": "in-1", "listen": "127.0.0.1", "port": 20000, "protocol": "socks",
                       "settings": {"auth": "noauth", "udp": False}}
    assert config["routing"]["rules"] == [
        {"type": "field", "ruleTag": "rule-1", "inboundTag": ["in-1"], "outboundTag": "out-1"},
        {"type": "field", "ruleTag": "rule-2", "inboundTag": ["in-2"], "outboundTag": "out-2"},
    ]


def test_outbound_is_copied_not_mutated():
    entry = entries()[0]
    config = build_config([entry], api_port=10085)
    assert "tag" not in entry.outbound
    assert config["outbounds"][1]["streamSettings"]["network"] == "ws"


def test_api_payloads_match_full_config():
    entry = entries()[1]
    full = build_config([entry], api_port=1)
    assert api_outbound_payload(entry)["outbounds"][0] == full["outbounds"][1]
    assert api_inbound_payload(entry)["inbounds"][0] == full["inbounds"][0]
    assert api_rule_payload(entry)["routing"]["rules"][0] == full["routing"]["rules"][0]


def test_entry_equality_detects_changes():
    a = entries()[0]
    same = XrayEntry.create(1, 20000, a.outbound)
    moved = XrayEntry.create(1, 20005, a.outbound)
    assert a == same and a != moved and a.fingerprint != moved.fingerprint


def test_validation_config_contains_only_outbounds():
    config = build_test_config(entries()[0].outbound)
    assert "inbounds" not in config and len(config["outbounds"]) == 2
