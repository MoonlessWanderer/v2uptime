"""Update and restart from the admin panel (Settings → Maintenance).

Restart: the panel process stops itself and systemd starts it again
(``Restart=always`` in the v2uptime unit). No extra rights are needed.

Update: needs root, like menu item 2 of ``sudo v2uptime``. The panel only
writes the word ``update`` to ``data/control-request``. The root unit
``v2uptime-control.path`` (set up by install.sh) sees the file and runs
``install.sh --control``, which runs the same update as the menu: download,
new virtualenv, swap, health check, rollback on failure. Progress goes to
``control/status.json`` and ``control/last.log``, owned by root and readable
by the panel. Nothing the panel writes can change what is downloaded or run.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from app.timeutil import now_ts

REQUEST_WORD = "update\n"
STALE_AFTER = 3600  # a "running" update older than this was interrupted (reboot, kill)
LOG_TAIL_BYTES = 24_000


class ControlError(Exception):
    """A message that is safe to show the admin."""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Control:
    def __init__(self, base_dir: Path) -> None:
        self.request_file = base_dir / "data" / "control-request"
        self.control_dir = base_dir / "control"

    # ------------------------------------------------------------- update
    def helper(self) -> dict[str, str] | None:
        """``{"repo": ..., "branch": ...}`` once install.sh has set up the root helper, else None."""
        try:
            data = json.loads((self.control_dir / "ready").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return {"repo": str(data.get("repo") or ""), "branch": str(data.get("branch") or "")}

    def log_tail(self) -> str:
        try:
            with open(self.control_dir / "last.log", "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                data = handle.read()
        except OSError:
            return ""
        text = data.decode("utf-8", "replace")
        if size > LOG_TAIL_BYTES:
            text = "…" + text.split("\n", 1)[-1]
        return text.rstrip()

    def update_status(self, now: int | None = None) -> dict[str, Any]:
        """Last update run from the panel. ``state``: running / ok / failed / interrupted / None."""
        try:
            data = json.loads((self.control_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        state = data.get("state") if data.get("state") in ("running", "ok", "failed") else None
        started = _int(data.get("started_at"))
        if state == "running" and started is not None and (now or now_ts()) - started > STALE_AFTER:
            state = "interrupted"
        return {
            "state": state,
            "started_at": started,
            "finished_at": _int(data.get("finished_at")),
            "message": str(data.get("message") or "") if state != "interrupted"
            else "The last update was interrupted before it finished.",
            "pending": self.request_file.exists(),
            "log": self.log_tail(),
        }

    def busy(self) -> bool:
        status = self.update_status()
        return status["pending"] or status["state"] == "running"

    def request_update(self) -> int:
        """Ask the root helper to update. Returns the request time (epoch)."""
        if self.helper() is None:
            raise ControlError("Updating from the panel is not set up yet. Run sudo v2uptime once on the server.")
        if self.busy():
            raise ControlError("An update is already running.")
        self.request_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.request_file.with_name(self.request_file.name + ".tmp")
        tmp.write_text(REQUEST_WORD, encoding="ascii")
        os.replace(tmp, self.request_file)  # appears complete, so the helper never reads half a file
        return now_ts()

    # ------------------------------------------------------------ restart
    @staticmethod
    def can_restart() -> bool:
        """True when systemd runs this process directly (it starts it again after it exits)."""
        return bool(os.environ.get("INVOCATION_ID")) and os.getppid() == 1

    def schedule_restart(self, delay: float = 1.0) -> None:
        """Stop the panel shortly after the response is sent; systemd starts it again."""
        if not self.can_restart():
            raise ControlError("Restart works when the panel runs as the v2uptime systemd service.")
        if self.busy():
            raise ControlError("An update is running; it restarts the panel when it is done.")
        asyncio.get_running_loop().call_later(delay, self._terminate)

    @staticmethod
    def _terminate() -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # graceful: uvicorn shuts down, Xray is stopped
