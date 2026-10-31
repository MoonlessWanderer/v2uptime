"""Pure builders for the Xray JSON config and the hot-reload API payloads.

Layout: one SOCKS inbound per server on 127.0.0.1:<socks_port> tagged
``in-<id>``, routed by a rule tagged ``rule-<id>`` to the server's outbound
``out-<id>``.  The first outbound is a blackhole, so traffic from an inbound
without a rule can never leak out directly and produce a false "online".
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

API_TAG = "api"
BLOCK_TAG = "block"
LISTEN = "127.0.0.1"


def inbound_tag(server_id: int) -> str:
    return f"in-{server_id}"


def outbound_tag(server_id: int) -> str:
    return f"out-{server_id}"


def rule_tag(server_id: int) -> str:
    return f"rule-{server_id}"


@dataclass(frozen=True)
class XrayEntry:
    """One server as loaded into Xray."""

    server_id: int
    port: int
    outbound_json: str  # canonical JSON of the outbound (without tag)

    @classmethod
    def create(cls, server_id: int, port: int, outbound: dict[str, Any]) -> "XrayEntry":
        return cls(server_id, port, json.dumps(outbound, sort_keys=True, separators=(",", ":")))

    @property
    def outbound(self) -> dict[str, Any]:
        return json.loads(self.outbound_json)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"{self.port}|{self.outbound_json}".encode()).hexdigest()


def build_inbound(entry: XrayEntry) -> dict[str, Any]:
    return {
        "tag": inbound_tag(entry.server_id),
        "listen": LISTEN,
        "port": entry.port,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
    }


def build_outbound(entry: XrayEntry) -> dict[str, Any]:
    outbound = copy.deepcopy(entry.outbound)
    outbound["tag"] = outbound_tag(entry.server_id)
    return outbound


def build_rule(entry: XrayEntry) -> dict[str, Any]:
    return {
        "type": "field",
        "ruleTag": rule_tag(entry.server_id),
        "inboundTag": [inbound_tag(entry.server_id)],
        "outboundTag": outbound_tag(entry.server_id),
    }


def build_config(entries: list[XrayEntry], api_port: int, log_level: str = "warning") -> dict[str, Any]:
    return {
        "log": {"loglevel": log_level, "access": "none"},
        "api": {"tag": API_TAG, "listen": f"{LISTEN}:{api_port}", "services": ["HandlerService", "RoutingService"]},
        "inbounds": [build_inbound(e) for e in entries],
        "outbounds": [{"tag": BLOCK_TAG, "protocol": "blackhole"}] + [build_outbound(e) for e in entries],
        "routing": {"domainStrategy": "AsIs", "rules": [build_rule(e) for e in entries]},
    }


def build_test_config(outbound: dict[str, Any]) -> dict[str, Any]:
    """Minimal config used with ``xray run -test`` to validate one outbound."""
    entry = XrayEntry.create(0, 1, outbound)
    return {
        "log": {"loglevel": "error"},
        "outbounds": [{"tag": BLOCK_TAG, "protocol": "blackhole"}, build_outbound(entry)],
    }


# Payloads for `xray api ado|adi|adrules` (each file is a partial config).

def api_outbound_payload(entry: XrayEntry) -> dict[str, Any]:
    return {"outbounds": [build_outbound(entry)]}


def api_inbound_payload(entry: XrayEntry) -> dict[str, Any]:
    return {"inbounds": [build_inbound(entry)]}


def api_rule_payload(entry: XrayEntry) -> dict[str, Any]:
    return {"routing": {"rules": [build_rule(entry)]}}
