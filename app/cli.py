"""Command line entry point: run the server and small admin tasks.

    python3 main.py                         # run on 127.0.0.1:8080 (single worker)
    python3 main.py create-admin -u admin   # prompts for the password twice
    python3 main.py reset-password -u admin
    python3 main.py set-setting admin_path /admin-x7k2
    python3 main.py backup-db -o /root/uptime-backup.db
    python3 main.py probe 3 --count 5         # cold vs warm timing through server 3
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from app import __version__
from app.config import get_config
from app.logging_setup import setup_logging


def _read_password(from_stdin: bool) -> str:
    from app.security import validate_password

    if from_stdin:
        lines = sys.stdin.read().splitlines()
        password, confirm = (lines + ["", ""])[:2]
        confirm = confirm or password
    else:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Repeat password: ")
    error = validate_password(password, confirm)
    if error:
        raise SystemExit(error)
    return password


def _open_db() -> None:
    from app.db import init_db
    from app.services.settings import settings

    init_db(get_config().db_path)
    settings.load()


def cmd_run(_: argparse.Namespace) -> None:
    import uvicorn

    from app.main import create_app

    cfg = get_config()
    setup_logging(cfg.log_dir, cfg.log_level)
    lock = _single_instance_lock(cfg.data_dir)
    app = create_app(cfg)
    # Exactly one worker: the scheduler runs in-process; more workers would
    # run every check and Telegram message more than once.
    uvicorn.run(app, host=cfg.host, port=cfg.port, workers=1, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_config=None, access_log=False, server_header=False)
    del lock


def _single_instance_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / ".lock", "w")  # noqa: SIM115 - held for the process lifetime
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("Another instance is already running (data/.lock is held).") from None
    return handle


def cmd_create_admin(args: argparse.Namespace) -> None:
    from app.models import Admin
    from app.security import hash_password

    _open_db()
    if Admin.get_or_none(Admin.username == args.username):
        raise SystemExit(f"Admin '{args.username}' already exists; use reset-password.")
    Admin.create(username=args.username, password_hash=hash_password(_read_password(args.password_stdin)))
    print(f"Admin '{args.username}' created.")


def cmd_reset_password(args: argparse.Namespace) -> None:
    from app.models import Admin
    from app.security import hash_password
    from app.timeutil import now_ts

    _open_db()
    admin = Admin.get_or_none(Admin.username == args.username)
    if admin is None:
        password = _read_password(args.password_stdin)
        Admin.create(username=args.username, password_hash=hash_password(password))
        print(f"Admin '{args.username}' did not exist and was created.")
        return
    admin.password_hash = hash_password(_read_password(args.password_stdin))
    admin.password_changed_at = max(now_ts(), admin.password_changed_at + 1)  # logs out every session
    admin.save()
    print(f"Password of '{args.username}' changed.")


def cmd_set_setting(args: argparse.Namespace) -> None:
    from app.services.settings import SettingError, settings

    _open_db()
    try:
        settings.set(args.key, args.value)
    except (SettingError, KeyError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"{args.key} = {settings.get(args.key)}")


def cmd_get_setting(args: argparse.Namespace) -> None:
    from app.services.settings import settings

    _open_db()
    print(settings.get(args.key))


def cmd_backup_db(args: argparse.Namespace) -> None:
    """Consistent copy using SQLite's online backup API (safe while running)."""
    source = sqlite3.connect(str(get_config().db_path))
    target = sqlite3.connect(args.out)
    with target:
        source.backup(target)
    source.close()
    target.close()
    print(f"Backup written to {args.out}")


PHASES = (("connect_tcp", "tcp"), ("setup_socks5_connection", "socks"), ("start_tls", "tls"),
          ("receive_response_headers", "ttfb"))


def _phase_text(phases: dict[str, int]) -> str:
    parts = [f"{label} {phases[key]}" for key, label in PHASES if key in phases]
    return f" ({', '.join(parts)})" if parts else ""


def _print_dns(address: str) -> None:
    from app.services.dns import is_ip, resolve
    from app.timeutil import fmt_datetime

    if is_ip(address):
        return
    from app.services.settings import settings

    answer = resolve(address)
    when = fmt_datetime(answer.at, settings.tz)
    if answer.ips:
        print(f"DNS: {', '.join(answer.ips)} via {answer.source} · {when}")
    elif answer.blocked:
        print(f"DNS: DNS blocked (filtered answer) via {answer.source} · {when}")
    else:
        print(f"DNS: DNS error ({answer.error or 'no answer'}) · {when}")


def _dns_failure(address: str) -> str | None:
    from app.services.dns import failure_kind, is_ip

    if is_ip(address):
        return None
    return failure_kind(address)


def cmd_probe(args: argparse.Namespace) -> None:
    """Show where a server's ping comes from: cold vs warm request, with connection phases."""
    import asyncio
    import statistics

    from app.models import Server
    from app.services.checker import error_label, probe, socks_url
    from app.services.settings import settings
    from app.services.xray_manager import port_is_free

    _open_db()
    server = Server.get_or_none(Server.id == args.server_id)
    if server is None:
        raise SystemExit(f"No server with id {args.server_id}.")
    if not server.socks_port or port_is_free(server.socks_port):
        raise SystemExit("This server is not loaded in Xray. Is the panel running and the server active?")
    url, timeout = settings.get("test_url"), float(settings.get("check_timeout"))
    proxy = socks_url(server.socks_port)
    print(f"Server {server.id}: {server.name} ({server.protocol}/{server.transport}/{server.security})")
    _print_dns(server.address)
    print(f"Test URL: {url} · timeout {timeout:g}s each · via {proxy}")
    print("Phases in ms: tcp = to local Xray, socks = SOCKS handshake, tls = TLS to the test host through "
          "the tunnel, ttfb = request sent to response headers.")
    colds, warms = [], []
    for attempt in range(1, args.count + 1):
        timers: list = []
        result = asyncio.run(probe(url, proxy, timeout, warm=True, timers=timers))
        if not result.ok:
            kind = _dns_failure(server.address)
            print(f"#{attempt}  failed: {error_label(kind or result.error_type)} · {result.detail or ''}")
            continue
        colds.append(result.connect_ms)
        line = f"#{attempt}  cold {result.connect_ms} ms{_phase_text(timers[0].phases)}"
        if result.warm:
            warms.append(result.latency_ms)
            line += f"   warm {result.latency_ms} ms{_phase_text(timers[1].phases)}"
        else:
            line += "   warm request failed (the check would use the cold time)"
        print(line + f"   HTTP {result.status_code}")
    if colds:
        summary = f"Median: cold {statistics.median(colds):.0f} ms"
        if warms:
            summary += f", warm {statistics.median(warms):.0f} ms"
        print(summary)
        print("The panel shows the warm time as Ping and the cold time as Connect time.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="v2uptime", description=f"v2uptime {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the web server (default)").set_defaults(func=cmd_run)
    for name, func, help_text in (("create-admin", cmd_create_admin, "create an admin user"),
                                  ("reset-password", cmd_reset_password, "reset (or create) an admin password")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-u", "--username", required=True)
        p.add_argument("--password-stdin", action="store_true", help="read password (and confirmation) from stdin")
        p.set_defaults(func=func)
    p = sub.add_parser("set-setting", help="change a runtime setting")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_set_setting)
    p = sub.add_parser("get-setting", help="print a runtime setting")
    p.add_argument("key")
    p.set_defaults(func=cmd_get_setting)
    p = sub.add_parser("probe", help="time cold and warm requests through one server (panel must be running)")
    p.add_argument("server_id", type=int)
    p.add_argument("--count", type=int, default=5)
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("backup-db", help="write a consistent copy of the database")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(func=cmd_backup_db)
    sub.add_parser("version").set_defaults(func=lambda _: print(__version__))
    args = parser.parse_args(argv)
    (getattr(args, "func", None) or cmd_run)(args)
