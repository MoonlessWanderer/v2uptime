"""Settings → Maintenance: update through the root helper, restart through systemd."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import control as control_mod
from app.services.control import Control, ControlError
from app.timeutil import now_ts

from tests.test_web import app_factory, client, csrf_from, login  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]


def ready(base: Path, repo="MoonlessWanderer/v2uptime", branch="main") -> Control:
    (base / "control").mkdir(parents=True, exist_ok=True)
    (base / "control" / "ready").write_text(json.dumps({"repo": repo, "branch": branch}), encoding="utf-8")
    return Control(base)


def write_status(base: Path, **fields) -> None:
    (base / "control").mkdir(parents=True, exist_ok=True)
    (base / "control" / "status.json").write_text(json.dumps({"action": "update", **fields}), encoding="utf-8")


# ------------------------------------------------------------------ Control

def test_update_needs_the_root_helper(tmp_path):
    control = Control(tmp_path)
    assert control.helper() is None
    with pytest.raises(ControlError, match="sudo v2uptime"):
        control.request_update()
    assert not control.request_file.exists()


def test_update_request_is_one_word_written_atomically(tmp_path):
    control = ready(tmp_path)
    assert control.helper() == {"repo": "MoonlessWanderer/v2uptime", "branch": "main"}
    assert abs(control.request_update() - now_ts()) <= 2
    assert control.request_file.read_text(encoding="ascii") == "update\n"
    assert [p.name for p in control.request_file.parent.iterdir()] == ["control-request"]  # no temp file left
    with pytest.raises(ControlError, match="already running"):
        control.request_update()


def test_update_status(tmp_path):
    control = ready(tmp_path)
    assert control.update_status()["state"] is None
    now = now_ts()
    write_status(tmp_path, state="running", started_at=now - 30, finished_at=None, message="Updating")
    assert control.update_status(now)["state"] == "running"
    with pytest.raises(ControlError):
        control.request_update()
    stale = control.update_status(now + 2 * 3600)
    assert stale["state"] == "interrupted" and "interrupted" in stale["message"]
    write_status(tmp_path, state="ok", started_at=now - 90, finished_at=now - 20, message="Updated to version 1.2.0.")
    status = control.update_status(now)
    assert (status["state"], status["finished_at"], status["message"]) == ("ok", now - 20, "Updated to version 1.2.0.")
    write_status(tmp_path, state="<script>", started_at="x")
    assert control.update_status(now)["state"] is None and control.update_status(now)["started_at"] is None


def test_log_tail_keeps_the_end(tmp_path):
    control = ready(tmp_path)
    lines = [f"line {i}" for i in range(5000)]
    (tmp_path / "control" / "last.log").write_text("\n".join(lines), encoding="utf-8")
    tail = control.log_tail()
    assert tail.startswith("…line ") and tail.endswith("line 4999")
    assert len(tail) <= control_mod.LOG_TAIL_BYTES + 1


def test_restart_only_under_systemd(tmp_path, monkeypatch):
    control = Control(tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert not control.can_restart()
    with pytest.raises(ControlError, match="systemd"):
        asyncio.run(_schedule(control))

    calls = []
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(control_mod.os, "getppid", lambda: 1)
    monkeypatch.setattr(Control, "_terminate", staticmethod(lambda: calls.append("SIGTERM")))
    assert control.can_restart()
    asyncio.run(_schedule(control, wait=0.1))
    assert calls == ["SIGTERM"]

    ready(tmp_path)
    write_status(tmp_path, state="running", started_at=now_ts())
    with pytest.raises(ControlError, match="update is running"):
        asyncio.run(_schedule(control))


async def _schedule(control, wait=0.0):
    control.schedule_restart(delay=0.01)
    await asyncio.sleep(wait)


# ------------------------------------------------------------------- routes

def test_maintenance_page_and_endpoints(client, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert client.get("/admin/maintenance/status.json", headers={"Accept": "application/json"}).status_code == 401
    login(client)
    services = client.app.state.services
    services.control = Control(tmp_path)

    page = client.get("/admin/maintenance").text
    assert "Update panel" in page and "Restart panel" in page and "sudo v2uptime" in page
    assert re.search(r'data-maintenance="update"[^>]*disabled', page)
    headers = {"X-CSRF-Token": csrf_from(page)}

    assert client.post("/admin/maintenance/update", json={}).status_code == 400  # no CSRF token
    response = client.post("/admin/maintenance/update", json={}, headers=headers)
    assert response.status_code == 409 and "sudo v2uptime" in response.json()["error"]
    response = client.post("/admin/maintenance/restart", json={}, headers=headers)
    assert response.status_code == 409 and "systemd" in response.json()["error"]

    ready(tmp_path, repo="someone/fork", branch="dev")
    page = client.get("/admin/maintenance").text
    assert "github.com/someone/fork (dev)" in page
    assert not re.search(r'data-maintenance="update"[^>]*disabled', page)
    response = client.post("/admin/maintenance/update", json={}, headers=headers)
    assert response.status_code == 200 and response.json()["ok"] is True
    assert (tmp_path / "data" / "control-request").read_text(encoding="ascii") == "update\n"
    info = client.get("/admin/maintenance/status.json").json()
    assert info["update"]["pending"] is True and info["helper"]["repo"] == "someone/fork"
    assert info["process_started_at"] == services.started_at
    assert client.post("/admin/maintenance/update", json={}, headers=headers).status_code == 409

    (tmp_path / "data" / "control-request").unlink()
    write_status(tmp_path, state="failed", started_at=now_ts() - 60, finished_at=now_ts(),
                 message="The update failed; version 1.1.0 is running. See the log.")
    (tmp_path / "control" / "last.log").write_text("[i] Installed: 1.1.0   New: 1.2.0\nboom\n", encoding="utf-8")
    page = client.get("/admin/maintenance").text
    assert "Failed</span>" in page and "version 1.1.0 is running" in page
    assert "boom" in page and "<details class=\"advanced\" open>" in page


# ------------------------------------------------- install.sh --control

def _bash() -> str | None:
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")  # Git Bash, not the WSL launcher in System32
    for parent in Path(git).parents if git else ():
        for candidate in (parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


BASH = _bash()
HARNESS = """
require_root() { :; }
chown() { :; }
systemctl() { echo "systemctl $*" >> "$INSTALL_DIR/calls"; }
do_update() {  # stands in for the real download and swap
  echo "fake update output"
  [[ "${FAKE_UPDATE_FAIL:-}" == 1 ]] && return 1
  sed -i 's/1.1.0/1.2.0/' "$INSTALL_DIR/app/__init__.py"
}
main "$@"
"""


def _install_tree(tmp_path: Path, tail: str = HARNESS) -> Path:
    base = tmp_path / "opt"
    (base / "app").mkdir(parents=True)
    (base / "data").mkdir()
    (base / "main.py").write_text("", encoding="utf-8")
    (base / "config.py").write_text("", encoding="utf-8")
    (base / "app" / "__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    script = script.replace('INSTALL_DIR="/opt/v2uptime"', f'INSTALL_DIR="{base.as_posix()}"', 1)
    assert script.rstrip().endswith('main "$@"')
    (tmp_path / "install-test.sh").write_text(script.rstrip()[: -len('main "$@"')] + tail, encoding="utf-8",
                                               newline="\n")
    return base


def _run_control(tmp_path: Path, request: bytes | None, **env) -> Path:
    base = tmp_path / "opt"
    if request is not None:
        (base / "data" / "control-request").write_bytes(request)
    subprocess.run([BASH, str(tmp_path / "install-test.sh"), "--control"], check=True, timeout=60,
                   env={**os.environ, **env}, capture_output=True)
    assert not (base / "data" / "control-request").exists()
    return base


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_install_control_runs_only_the_update_request(tmp_path):
    base = _install_tree(tmp_path)
    for junk in (b"rm -rf /\n", b"restart", b""):
        _run_control(tmp_path, junk)
        assert not (base / "control" / "status.json").exists()
    assert "1.1.0" in (base / "app" / "__init__.py").read_text(encoding="utf-8")

    _run_control(tmp_path, b"update\n")
    status = json.loads((base / "control" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "ok" and status["version"] == "1.2.0"
    assert status["message"] == "Updated to version 1.2.0." and status["finished_at"] >= status["started_at"]
    assert "fake update output" in (base / "control" / "last.log").read_text(encoding="utf-8")
    assert Control(base).update_status()["state"] == "ok"


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_install_control_reports_a_failed_update(tmp_path):
    base = _install_tree(tmp_path)
    _run_control(tmp_path, b"update", FAKE_UPDATE_FAIL="1")
    status = Control(base).update_status()
    assert status["state"] == "failed" and "1.1.0 is running" in status["message"]


DB_HARNESS = r"""
install() { mkdir -p "${@: -1}"; printf 'install %s\n' "$*" >> "$INSTALL_DIR/calls"; }
app_cmd() {
  if [[ "${FAKE_BACKUP_FAIL:-}" == 1 ]]; then
    echo "permission denied" >&2
    return 1
  fi
  local out=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-o" || "$1" == "--out" ]]; then out="$2"; shift; fi
    shift
  done
  printf 'consistent\n' > "$out"
}
systemctl() { printf 'systemctl %s\n' "$*" >> "$INSTALL_DIR/calls"; }

mkdir -p "$INSTALL_DIR/data"
printf 'live-db\n' > "$INSTALL_DIR/data/uptime.db"
printf 'wal-bytes\n' > "$INSTALL_DIR/data/uptime.db-wal"
printf 'shm-bytes\n' > "$INSTALL_DIR/data/uptime.db-shm"
rollback="$INSTALL_DIR/.rollback"
rm -rf "$rollback"
mkdir -p "$rollback"
: > "$INSTALL_DIR/calls"
backup_database "$rollback/db"
if [[ "${DB_BACKUP_OK:-0}" != 1 ]]; then
  [[ -f "$rollback/db/uptime.db" ]] && exit 2
  systemctl stop "$SERVICE"
  snapshot_database_files "$rollback/db"
fi
printf 'migrated\n' > "$INSTALL_DIR/data/uptime.db"
rm -f "$INSTALL_DIR/data/uptime.db-wal" "$INSTALL_DIR/data/uptime.db-shm"
printf 'new-wal\n' > "$INSTALL_DIR/data/uptime.db-wal"
restore_database_backup "$rollback/db"
"""


def _db_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_update_backup_uses_a_service_owned_directory(tmp_path):
    base = _install_tree(tmp_path, DB_HARNESS)
    result = subprocess.run([BASH, str(tmp_path / "install-test.sh")], check=True, timeout=60,
                            capture_output=True, text=True, encoding="utf-8")
    calls = (base / "calls").read_text(encoding="utf-8")
    assert "install -d -o v2uptime -m 700 " in calls
    assert _db_text(base / ".rollback" / "db" / "uptime.db") == "consistent\n"
    assert not (base / ".rollback" / "db" / "uptime.db-wal").exists()
    assert "Database backup failed" not in result.stderr
    assert _db_text(base / "data" / "uptime.db") == "consistent\n"
    assert not (base / "data" / "uptime.db-wal").exists()  # migrated WAL is not kept


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_update_backup_falls_back_after_stop_and_restores_wal(tmp_path):
    base = _install_tree(tmp_path, DB_HARNESS)
    result = subprocess.run([BASH, str(tmp_path / "install-test.sh")], check=True, timeout=60,
                            capture_output=True, text=True, encoding="utf-8", env={**os.environ, "FAKE_BACKUP_FAIL": "1"})
    assert "Database backup failed" in result.stderr
    assert "uptime.db-wal and uptime.db-shm" in result.stderr
    calls = (base / "calls").read_text(encoding="utf-8")
    assert "systemctl stop v2uptime" in calls
    assert _db_text(base / ".rollback" / "db" / "uptime.db") == "live-db\n"
    assert _db_text(base / ".rollback" / "db" / "uptime.db-wal") == "wal-bytes\n"
    assert _db_text(base / ".rollback" / "db" / "uptime.db-shm") == "shm-bytes\n"
    assert _db_text(base / "data" / "uptime.db") == "live-db\n"
    assert _db_text(base / "data" / "uptime.db-wal") == "wal-bytes\n"
    assert _db_text(base / "data" / "uptime.db-shm") == "shm-bytes\n"


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_install_setup_control_writes_units_and_marker(tmp_path):
    base = _install_tree(tmp_path)
    (base / "data" / "control-request").write_text("update\n", encoding="ascii")  # left over: must be dropped
    units = tmp_path / "units"
    units.mkdir()
    script = tmp_path / "install-test.sh"
    script.write_text(script.read_text(encoding="utf-8").replace("/etc/systemd/system/", units.as_posix() + "/"),
                      encoding="utf-8", newline="\n")
    subprocess.run([BASH, str(script), "--setup-control"], check=True, timeout=60, capture_output=True,
                   env={**os.environ, "V2UPTIME_REPO": "someone/fork", "V2UPTIME_BRANCH": "dev"})
    path_unit = (units / "v2uptime-control.path").read_text(encoding="utf-8")
    service_unit = (units / "v2uptime-control.service").read_text(encoding="utf-8")
    assert f"PathExists={base.as_posix()}/data/control-request" in path_unit
    assert f"ExecStart={base.as_posix()}/install.sh --control" in service_unit
    assert "V2UPTIME_REPO=someone/fork V2UPTIME_BRANCH=dev" in service_unit
    assert Control(base).helper() == {"repo": "someone/fork", "branch": "dev"}
    assert not (base / "data" / "control-request").exists()
    assert "enable --now v2uptime-control.path" in (base / "calls").read_text(encoding="utf-8")
