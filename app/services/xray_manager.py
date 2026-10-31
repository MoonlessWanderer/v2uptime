"""Run and supervise a single Xray process that holds every active server.

* Hot changes use ``xray api ado|adrules|adi`` and ``rmi|rmrules|rmo`` (the
  official CLI wrappers around HandlerService / RoutingService gRPC), so
  adding or editing one server never interrupts the others.
* If a hot change fails, or the process crashes, a full restart is done:
  ``epoch`` is bumped and the ready flag cleared, the checker pauses, and
  results from a cycle that overlapped a restart are discarded.
* Outbounds rejected by Xray are marked on the server (``xray_error``) and
  excluded, so one bad link can never keep Xray from starting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from app.config import Config
from app.db import db, run_db
from app.models import Server
from app.services.dns import xray_dns_options
from app.services.xray_config import (
    XrayEntry,
    api_inbound_payload,
    api_outbound_payload,
    api_rule_payload,
    build_config,
    build_test_config,
    inbound_tag,
    outbound_tag,
    rule_tag,
)
from app.timeutil import now_ts

log = logging.getLogger(__name__)

HOT_RELOAD_LIMIT = 40  # more changes than this -> one full restart is faster
PORT_SCAN_SPAN = 20000

EventCallback = Callable[[str, dict[str, Any]], None]


class HotReloadError(RuntimeError):
    pass


# ----------------------------------------------------------- port helpers

def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            # Same semantics as Go's net.Listen; ignores TIME_WAIT leftovers.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _next_free_port(base: int, reserved: set[int]) -> int:
    for port in range(base, min(base + PORT_SCAN_SPAN, 65535)):
        if port not in reserved and port_is_free(port):
            return port
    raise RuntimeError("No free port for an inbound.")


def desired_entries(port_base: int, api_port: int, keep_ports: set[int]) -> list[XrayEntry]:
    """Active servers Xray should load; allocates stable, free SOCKS ports.

    Ports in ``keep_ports`` are currently held by our own Xray process and are
    not re-checked; every other port must be bindable or is reassigned.
    """
    servers = list(
        Server.select().where(Server.is_active == True, Server.xray_error.is_null()).order_by(Server.id)  # noqa: E712
    )
    reserved = {
        p for (p,) in Server.select(Server.socks_port).where(Server.socks_port.is_null(False)).tuples()
    } | {api_port}
    entries: list[XrayEntry] = []
    with db.atomic():
        for server in servers:
            port = server.socks_port
            if port is None or port == api_port or (port not in keep_ports and not port_is_free(port)):
                new_port = _next_free_port(port_base, reserved)
                if port is not None:
                    log.warning("socks port busy, reassigning", extra={"server_id": server.id, "old": port,
                                                                        "new": new_port})
                reserved.add(new_port)
                server.socks_port = new_port
                server.save(only=[Server.socks_port])
                port = new_port
            try:
                entries.append(XrayEntry.create(server.id, port, server.outbound))
            except (TypeError, ValueError):
                Server.update(xray_error="Stored outbound is corrupt").where(Server.id == server.id).execute()
    return entries


def mark_xray_errors(errors: dict[int, str]) -> None:
    with db.atomic():
        for server_id, message in errors.items():
            Server.update(xray_error=message[:500]).where(Server.id == server_id).execute()


def _short_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if "failed" in line.lower() or "error" in line.lower() or "invalid" in line.lower():
            return line[-300:]
    return (lines[-1] if lines else "unknown error")[-300:]


# --------------------------------------------------------------- manager

class XrayManager:
    def __init__(self, cfg: Config, on_event: EventCallback | None = None) -> None:
        self.cfg = cfg
        self.bin: Path = cfg.xray_bin
        self.run_dir: Path = cfg.xray_run_dir
        self.config_path = self.run_dir / "config.json"
        self.pid_path = self.run_dir / "xray.pid"
        self.api_addr = f"127.0.0.1:{cfg.xray_api_port}"
        self._on_event = on_event or (lambda kind, data: None)
        self._proc: asyncio.subprocess.Process | None = None
        self._loaded: dict[int, XrayEntry] = {}
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stopping = False
        self._hot_disabled_until = 0.0
        self.epoch = 0
        self.state = "stopped"  # stopped / running / restarting / crashed / error / missing
        self.version: str | None = None
        self.started_at: int | None = None
        self.restarts = 0
        self.crashes = 0
        self.last_error: str | None = None
        self.output: deque[str] = deque(maxlen=200)

    # ---------------------------------------------------------- queries
    def argv(self, *args: str) -> list[str]:
        """Command line for the Xray binary (single place; tests substitute a fake)."""
        return [str(self.bin), *args]

    @property
    def installed(self) -> bool:
        return self.bin.is_file()

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._proc is not None and self._proc.returncode is None

    async def wait_ready(self, timeout: float) -> bool:
        if self.is_ready():
            return True
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.is_ready()

    def loaded_ports(self) -> dict[int, int]:
        """server_id -> local SOCKS port, only while Xray is ready."""
        if not self.is_ready():
            return {}
        return {sid: entry.port for sid, entry in self._loaded.items()}

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state if self.installed else "missing",
            "ready": self.is_ready(),
            "installed": self.installed,
            "version": self.version,
            "pid": self._proc.pid if self._proc and self._proc.returncode is None else None,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "crashes": self.crashes,
            "loaded": len(self._loaded) if self.is_ready() else 0,
            "last_error": self.last_error,
            "epoch": self.epoch,
            "binary": str(self.bin),
        }

    # -------------------------------------------------------- lifecycle
    async def start(self) -> bool:
        self._stopping = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.installed:
            self.state = "missing"
            self.last_error = f"Xray binary not found: {self.bin}"
            log.error("xray binary not found", extra={"path": str(self.bin)})
            return False
        self.version = await self.read_version()
        async with self._lock:
            return await self._full_restart_locked("startup", initial=True)

    async def stop(self) -> None:
        self._stopping = True
        async with self._lock:
            self._ready.clear()
            await self._stop_process()
            self.state = "stopped"

    async def restart(self, reason: str) -> bool:
        async with self._lock:
            return await self._full_restart_locked(reason)

    async def supervise_forever(self, interval: float = 30.0) -> None:
        """Retry starting Xray if it is not running (e.g. the first start failed)."""
        while not self._stopping:
            await asyncio.sleep(interval)
            if self.installed and not self.is_ready() and not self._lock.locked() and self.state != "crashed":
                log.info("xray not running, retrying start")
                if self.version is None:
                    self.version = await self.read_version()
                async with self._lock:
                    await self._full_restart_locked("supervisor retry")

    async def reconcile(self) -> None:
        """Bring the running Xray in line with the database (hot when possible)."""
        if not self.installed or self._stopping:
            return
        async with self._lock:
            if not self.is_ready():
                await self._full_restart_locked("reconcile")
                return
            keep = {e.port for e in self._loaded.values()}
            desired = {
                e.server_id: e
                for e in await run_db(desired_entries, self.cfg.socks_port_base, self.cfg.xray_api_port, keep)
            }
            remove = [sid for sid, e in self._loaded.items() if desired.get(sid) != e]
            add = [sid for sid, e in desired.items() if self._loaded.get(sid) != e]
            if not remove and not add:
                return
            if len(remove) + len(add) > HOT_RELOAD_LIMIT or time.monotonic() < self._hot_disabled_until:
                await self._full_restart_locked(f"{len(remove) + len(add)} changes")
                return
            try:
                for sid in remove:
                    await self._hot_remove(self._loaded.pop(sid))
                rejected: dict[int, str] = {}
                for sid in add:
                    error = await self._hot_add(desired[sid])
                    if error:
                        rejected[sid] = error
                    else:
                        self._loaded[sid] = desired[sid]
                if rejected:
                    await run_db(mark_xray_errors, rejected)
                self._write_config(list(self._loaded.values()))
                added_ports = [desired[sid].port for sid in add if sid in self._loaded]
                if added_ports and not await self._wait_ports(added_ports, 5.0, self._proc):
                    raise HotReloadError("new inbounds did not open")
                log.info("xray hot reload", extra={"removed": len(remove), "added": len(add) - len(rejected),
                                                   "rejected": len(rejected)})
            except HotReloadError as exc:
                log.warning("hot reload failed, doing full restart", extra={"error": str(exc)[:300]})
                self._hot_disabled_until = time.monotonic() + 600
                await self._full_restart_locked("hot reload failed")

    async def validate_outbound(self, outbound: dict[str, Any]) -> str | None:
        """Return Xray's error message for an invalid outbound, else None."""
        if not self.installed:
            return None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"validate-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(build_test_config(outbound)), encoding="utf-8")
        try:
            ok, output = await self._exec(self.argv("run", "-test", "-c", str(path)), timeout=20)
        finally:
            path.unlink(missing_ok=True)
        return None if ok else _short_error(output)

    async def read_version(self) -> str | None:
        if not self.installed:
            return None
        ok, output = await self._exec(self.argv("version"), timeout=10)
        if ok and output.strip():
            parts = output.split()
            return parts[1] if len(parts) > 1 else parts[0]
        return None

    # --------------------------------------------------------- internals
    async def _full_restart_locked(self, reason: str, initial: bool = False) -> bool:
        self._ready.clear()
        self.epoch += 1
        self.state = "restarting"
        await self._stop_process()
        if self._stopping:
            return False
        log.info("xray (re)starting", extra={"reason": reason, "epoch": self.epoch})
        for attempt in range(3):
            entries = await run_db(desired_entries, self.cfg.socks_port_base, self.cfg.xray_api_port, set())
            self._write_config(entries)
            ok, output = await self._exec(self.argv("run", "-test", "-c", str(self.config_path)), timeout=30)
            if not ok:
                rejected: dict[int, str] = {}
                for entry in entries:
                    error = await self.validate_outbound(entry.outbound)
                    if error:
                        rejected[entry.server_id] = error
                if rejected and attempt < 2:
                    log.warning("xray rejected outbounds", extra={"servers": sorted(rejected)})
                    await run_db(mark_xray_errors, rejected)
                    continue
                self.last_error = _short_error(output)
                self.state = "error"
                log.error("xray config invalid", extra={"error": self.last_error})
                return False
            if await self._spawn(entries):
                if not initial:
                    self.restarts += 1
                return True
            await asyncio.sleep(1 + attempt)
        self.state = "error"
        return False

    def _write_config(self, entries: list[XrayEntry]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        servers, ipv4_only, _strategy = xray_dns_options()
        config = build_config(entries, self.cfg.xray_api_port, dns_servers=servers, ipv4_only=ipv4_only)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.config_path)

    async def _spawn(self, entries: list[XrayEntry]) -> bool:
        self._kill_stale_process()
        env = {**os.environ, "XRAY_LOCATION_ASSET": str(self.bin.parent)}
        proc = await asyncio.create_subprocess_exec(
            *self.argv("run", "-c", str(self.config_path)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.run_dir), env=env,
        )
        self._proc = proc
        self.pid_path.write_text(str(proc.pid), encoding="utf-8")
        asyncio.create_task(self._read_output(proc))
        ports = [self.cfg.xray_api_port] + [e.port for e in entries]
        if not await self._wait_ports(ports, 20.0, proc):
            tail = " | ".join(list(self.output)[-3:])
            self.last_error = f"Xray did not become ready: {tail}"[:500]
            log.error("xray failed to become ready", extra={"tail": tail[:300]})
            await self._stop_process()
            return False
        self._loaded = {e.server_id: e for e in entries}
        self.state = "running"
        self.started_at = now_ts()
        self.last_error = None
        self._ready.set()
        asyncio.create_task(self._watch(proc))
        log.info("xray ready", extra={"pid": proc.pid, "servers": len(entries), "epoch": self.epoch})
        return True

    async def _stop_process(self) -> None:
        proc, self._proc = self._proc, None
        self._loaded = {}
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        self.pid_path.unlink(missing_ok=True)

    def _kill_stale_process(self) -> None:
        """Kill an Xray left over from a previous panel run (holds our ports)."""
        if not self.pid_path.is_file() or sys.platform == "win32":
            return
        try:
            pid = int(self.pid_path.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except (OSError, ValueError):
            return
        if str(self.config_path) in cmdline:
            log.warning("killing stale xray process", extra={"pid": pid})
            try:
                os.kill(pid, 15)
                time.sleep(1)
                os.kill(pid, 9)
            except OSError:
                pass

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if self._stopping or proc is not self._proc:
            return
        self._ready.clear()
        self.epoch += 1
        self.crashes += 1
        self.state = "crashed"
        detail = list(self.output)[-1] if self.output else ""
        self.last_error = f"Xray stopped (exit code {code})"
        log.error("xray crashed", extra={"code": code, "last_line": detail[:300]})
        self._on_event("xray_crash", {"code": code, "detail": detail[:300]})
        delay = 1.0
        while not self._stopping:
            async with self._lock:
                if self.is_ready():
                    return
                ok = await self._full_restart_locked("crash recovery")
            if ok:
                self._on_event("xray_restarted", {"version": self.version})
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            self.output.append(line)
            if "[Error]" in line:
                log.warning("xray: %s", line[:500])
            else:
                log.debug("xray: %s", line[:500])

    async def _wait_ports(self, ports: list[int], timeout: float,
                          proc: asyncio.subprocess.Process | None) -> bool:
        deadline = time.monotonic() + timeout
        pending = set(ports)
        while pending and time.monotonic() < deadline:
            if proc is None or proc.returncode is not None:
                return False
            for port in list(pending):
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
                    writer.close()
                    pending.discard(port)
                except (OSError, asyncio.TimeoutError):
                    pass
            if pending:
                await asyncio.sleep(0.2)
        return not pending

    async def _api(self, command: str, *args: str, payload: dict[str, Any] | None = None) -> tuple[bool, str]:
        argv = self.argv("api", command, f"--server={self.api_addr}", *args)
        path: Path | None = None
        if payload is not None:
            path = self.run_dir / f"api-{uuid.uuid4().hex}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            argv.append(str(path))
        try:
            return await self._exec(argv, timeout=15)
        finally:
            if path:
                path.unlink(missing_ok=True)

    async def accepts_dns_config(self) -> tuple[bool, str]:
        """Whether this Xray accepts ``unexpectedIPs`` (25.6.8 or newer).

        A missing binary is treated as success: the setting is stored and
        applied the next time Xray starts. A rejected test config is not saved.
        """
        if not self.installed:
            return True, ""
        config = build_config([], self.cfg.xray_api_port, dns_servers=["127.0.0.1"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "dns-test.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        try:
            ok, output = await self._exec(self.argv("run", "-test", "-c", str(path)), timeout=30)
        finally:
            path.unlink(missing_ok=True)
        if ok:
            return True, ""
        if "unexpectedIPs" in output or "unknown field" in output.lower():
            return False, (
                "This Xray core does not support DNS filtering (unexpectedIPs). "
                "Update the Xray core (25.6.8 or newer) and try again."
            )
        return False, f"Xray rejected the DNS config: {_short_error(output)}"

    async def _hot_add(self, entry: XrayEntry) -> str | None:
        """Add outbound, rule, inbound (in that order).  Returns a rejection reason."""
        _servers, _ipv4, strategy = xray_dns_options()
        ok, output = await self._api("ado", payload=api_outbound_payload(entry, strategy))
        if not ok:
            invalid = await self.validate_outbound(entry.outbound)
            if invalid:
                return invalid
            raise HotReloadError(f"ado: {_short_error(output)}")
        ok, output = await self._api("adrules", "-append", payload=api_rule_payload(entry))
        if not ok:
            await self._api("rmo", outbound_tag(entry.server_id))
            raise HotReloadError(f"adrules: {_short_error(output)}")
        ok, output = await self._api("adi", payload=api_inbound_payload(entry))
        if not ok:
            await self._api("rmrules", rule_tag(entry.server_id))
            await self._api("rmo", outbound_tag(entry.server_id))
            raise HotReloadError(f"adi: {_short_error(output)}")
        return None

    async def _hot_remove(self, entry: XrayEntry) -> None:
        """Remove inbound first so no traffic can hit a missing rule."""
        for command, tag in (("rmi", inbound_tag(entry.server_id)), ("rmrules", rule_tag(entry.server_id)),
                             ("rmo", outbound_tag(entry.server_id))):
            ok, output = await self._api(command, tag)
            if not ok:
                log.debug("xray api remove failed", extra={"cmd": command, "tag": tag, "out": output[-200:]})
        # A port that is still open means rmi did not work: let reconcile restart.
        if not port_is_free(entry.port):
            await asyncio.sleep(0.3)
            if not port_is_free(entry.port):
                raise HotReloadError(f"inbound {inbound_tag(entry.server_id)} still listening")

    @staticmethod
    async def _exec(argv: list[str], timeout: float) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except OSError as exc:
            return False, str(exc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "timeout"
        return proc.returncode == 0, out.decode(errors="replace")
