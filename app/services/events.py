"""Domain events produced by monitoring and consumed by notifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DOWN = "down"
UP = "up"
FLAPPING = "flapping"
FLAP_END = "flap_end"
NETWORK_DOWN = "network_down"
NETWORK_UP = "network_up"
XRAY_CRASH = "xray_crash"
XRAY_RESTARTED = "xray_restarted"
PANEL_STARTED = "panel_started"

# Event kind -> the Telegram event category that enables it.
CATEGORY = {
    DOWN: "down",
    UP: "up",
    FLAPPING: "flapping",
    FLAP_END: "flapping",
    NETWORK_DOWN: "network",
    NETWORK_UP: "network",
    XRAY_CRASH: "system",
    XRAY_RESTARTED: "system",
    PANEL_STARTED: "system",
}


@dataclass
class Event:
    kind: str
    at: int
    server_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
