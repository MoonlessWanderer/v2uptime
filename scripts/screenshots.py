"""Rebuild the README screenshots (docs/screenshots/*.jpg) from sample data.

    python scripts/screenshots.py            # write the four images
    python scripts/screenshots.py --check    # layout and flag-font checks only, nothing is written

A temporary database gets sample servers and 24 hours of checks. The panel
runs on 127.0.0.1 with tests/fake_xray.py standing in for Xray; the checker
and the other background jobs stay off, so the sample data is shown as
written. Chromium (dark theme) is driven by Playwright:

    pip install playwright && python -m playwright install chromium

Nothing from the real config.py or database is read.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import random
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.services.container import Services  # noqa: E402
from app.services.xray_manager import XrayManager  # noqa: E402

FAKE_XRAY = ROOT / "tests" / "fake_xray.py"
OUT_DIR = ROOT / "docs" / "screenshots"
MAX_BYTES = 300 * 1024
UUID = "d342d11e-d424-4583-b36e-524ab1f0afa4"
PBK = "SbVKOEMjK0sIlbwg4akyBg5mL5KZwwB-ed4eEE7YnRc"
FLAG_FONT = "TwemojiCountryFlags.woff2"

# name, share link (example hosts only), ping ms, connect ms
SERVERS = [
    ("🇩🇪 Germany | Reality",
     f"vless://{UUID}@de.example.net:443?security=reality&sni=www.microsoft.com&fp=chrome&pbk={PBK}"
     "&sid=6ba85179e30d4fc2&type=tcp&flow=xtls-rprx-vision", 138, 512),
    ("Amsterdam WS 🇳🇱", f"vless://{UUID}@nl.example.net:443?security=tls&sni=nl.example.net&type=ws&path=%2Fws",
     162, 590),
    ("🇫🇮 Helsinki gRPC", f"trojan://secret@fi.example.net:443?security=tls&sni=fi.example.net&type=grpc"
     "&serviceName=gun", 205, 688),
    ("🇮🇷➡️🇩🇪 Tunnel", f"vless://{UUID}@ir.example.net:8443?security=none&type=tcp", 96, 301),
    ("🇺🇸 New York", f"trojan://secret@us.example.net:443?security=tls&sni=us.example.net&type=tcp", 312, 1105),
    ("🇬🇧 London", "ss://" + base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:secret").decode().rstrip("=")
     + "@uk.example.net:8388", 171, 560),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------- sample data

def seed(now: int) -> dict[str, int]:
    from app.db import db
    from app.models import Admin, Check, Incident, Server
    from app.services import servers as server_service
    from app.services.settings import settings

    rng = random.Random(7)
    settings.set("test_location", "Tehran")
    links = "\n".join(f"{link}#{quote(name)}" for name, link, _, _ in SERVERS)
    result = server_service.add_links(links)
    assert not result.errors, result.errors
    ids = {s.name: s.id for s in Server.select()}

    down_since = now - 47 * 60
    closed = (now - 3 * 3600 - 600, now - 3 * 3600 + 240)  # Helsinki: 14 minutes, 3 hours ago
    rows = []
    for name, _link, ping, connect in SERVERS:
        sid = ids[name]
        first = now - 6 * 60 if name == "🇬🇧 London" else now - 24 * 3600  # London is new
        for ts in range(first, now, 60):
            fail = False
            if name == "🇺🇸 New York":
                fail = ts >= down_since
            elif name == "🇫🇮 Helsinki gRPC":
                fail = closed[0] <= ts < closed[1]
            elif name == "🇮🇷➡️🇩🇪 Tunnel":
                fail = now - ts in (540, 360, 180) or rng.random() < 0.004  # degraded, never 3 in a row
            elif name == "Amsterdam WS 🇳🇱":
                fail = rng.random() < 0.002
            wave = 1 + 0.12 * math.sin(ts / 5400)
            latency = int(ping * wave + rng.gauss(0, ping * 0.07))
            rows.append({
                "server": sid, "checked_at": ts, "status": "fail" if fail else "ok",
                "latency_ms": None if fail else max(20, latency),
                "connect_ms": None if fail else max(60, int(connect * wave + rng.gauss(0, connect * 0.08))),
                "error_type": "timeout" if fail else None,
            })
    with db.atomic():
        for start in range(0, len(rows), 500):
            Check.insert_many(rows[start:start + 500]).execute()
        Incident.create(server=ids["🇫🇮 Helsinki gRPC"], started_at=closed[0], ended_at=closed[1],
                        duration_seconds=closed[1] - closed[0], error_type="timeout")
        Incident.create(server=ids["🇺🇸 New York"], started_at=down_since, error_type="timeout")
    from app.security import hash_password

    password = secrets.token_urlsafe(18)
    Admin.create(username="admin", password_hash=hash_password(password))
    ids["__password__"] = password  # type: ignore[assignment]
    return ids


# ------------------------------------------------------------------ server

class Panel:
    """The real app on 127.0.0.1, with the fake Xray started but no checks."""

    def __init__(self, tmp: Path) -> None:
        import uvicorn

        from app.main import create_app
        from app.timeutil import now_ts

        XrayManager.argv = lambda self, *args: [sys.executable, str(FAKE_XRAY), *args]
        original_start = Services.start

        async def start(self, background: bool = True) -> None:
            await original_start(self, background=False)
            await self.xray.start()

        Services.start = start
        base = random.randint(30000, 50000)
        self.port = free_port()
        cfg = Config(host="127.0.0.1", port=self.port, db_path=tmp / "uptime.db", secret_key=secrets.token_hex(16),
                     xray_bin=FAKE_XRAY, xray_api_port=base - 1, socks_port_base=base, log_dir=tmp / "logs")
        app = create_app(cfg, background=False)
        self.ids = seed(now_ts())
        self.password = self.ids.pop("__password__")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Panel":
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urlopen(self.url + "/healthz", timeout=2) as response:
                    if json.load(response).get("xray_ready"):
                        return self
            except OSError:
                pass
            time.sleep(0.2)
        raise SystemExit("The panel or the fake Xray did not start.")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


# ------------------------------------------------------------- screenshots

def save_jpeg(page, path: Path, **kwargs) -> None:
    for quality in (85, 78, 70, 62, 55):
        page.screenshot(path=str(path), type="jpeg", quality=quality, **kwargs)
        if path.stat().st_size <= MAX_BYTES:
            break
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB  (quality {quality})")


def settle(page) -> None:
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_timeout(300)


def take_screenshots(browser, panel: Panel) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    desktop = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    page = desktop.new_page()
    page.goto(panel.url + "/")
    settle(page)
    save_jpeg(page, OUT_DIR / "status-page.jpg", full_page=True)

    row = page.locator(f'#servers > li[data-id="{panel.ids["🇫🇮 Helsinki gRPC"]}"]')
    with page.expect_response(lambda r: "/history" in r.url):
        row.locator(".row-main").click()
    settle(page)
    worst = row.evaluate("""li => {
        const bars = [...li.querySelectorAll('.b')].filter(b => b.dataset.u !== '');
        bars.sort((a, b) => parseFloat(a.dataset.u) - parseFloat(b.dataset.u));
        return bars.length ? [...li.querySelectorAll('.b')].indexOf(bars[0]) : -1;
    }""")
    if worst >= 0:
        row.locator(".b").nth(worst).hover()
    save_jpeg(page, OUT_DIR / "server-detail.jpg", full_page=True)
    desktop.close()

    mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                 has_touch=True, color_scheme="dark")
    page = mobile.new_page()
    page.goto(panel.url + "/")
    settle(page)
    save_jpeg(page, OUT_DIR / "mobile.jpg", full_page=True)
    mobile.close()

    admin = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
    page = admin.new_page()
    page.goto(panel.url + "/admin/login")
    page.fill("#username", "admin")
    page.fill("#password", panel.password)
    with page.expect_navigation():
        page.click("button[type=submit]")
    page.goto(panel.url + "/admin/")
    settle(page)
    if "Xray binary not found" in page.content():
        raise SystemExit("The dashboard shows the missing-Xray alert.")
    save_jpeg(page, OUT_DIR / "admin-dashboard.jpg", full_page=True)
    admin.close()


# ------------------------------------------------------------------ checks

MEASURE = """() => {
    const box = (el) => el.getBoundingClientRect();
    const text = (el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect(); };
    const rows = [...document.querySelectorAll('#servers > li .row-main')];
    const head = document.querySelector('.list-head');
    const spans = head && getComputedStyle(head).display !== 'none' ? head.querySelectorAll('span') : null;
    return {
        scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
        head: spans ? {status: box(spans[1]).left, ping: box(spans[2]).right, up: box(spans[3]).right} : null,
        rows: rows.map((row) => {
            const [name, status, ping, up] = ['.row-server', '.row-status', '.row-ping', '.row-up']
                .map((s) => row.querySelector(s));
            return {
                nameWidth: box(name).width,
                status: box(status).left, statusText: text(status).left,
                statusFits: text(status).right <= box(status).right + 0.5,
                ping: box(ping).right, pingText: text(ping).right, pingFits: text(ping).left >= box(ping).left - 0.5,
                up: box(up).right, upText: text(up).right, upFits: text(up).left >= box(up).left - 0.5,
            };
        }),
    };
}"""

LONGEST = """() => {
    const rows = [...document.querySelectorAll('#servers > li .row-main')];
    const set = (row, status, ping, up) => {
        row.querySelector('.row-status').innerHTML = '<span class="status-dot degraded"></span>' + status;
        row.querySelector('.row-ping').textContent = ping;
        row.querySelector('.row-up').textContent = up;
    };
    set(rows[0], 'Degraded', '9999 ms', '99.99%');
    set(rows[1], 'Unknown', '—', '100%');
    rows[2].querySelector('.row-server bdi').textContent = '🇩🇪 A very long server name that must be cut short | Reality';
}"""


def check_layout(browser, panel: Panel, failures: list[str]) -> None:
    for width in (360, 390, 1280):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark",
                                      is_mobile=width < 700, has_touch=width < 700)
        page = context.new_page()
        page.goto(panel.url + "/")
        settle(page)
        page.evaluate(LONGEST)
        data = page.evaluate(MEASURE)
        rows = data["rows"]
        label = f"{width}px"
        if data["scrollWidth"] > data["innerWidth"]:
            failures.append(f"{label}: horizontal scroll ({data['scrollWidth']} > {data['innerWidth']})")
        for key in ("status", "statusText", "ping", "pingText", "up", "upText"):
            values = [r[key] for r in rows]
            if max(values) - min(values) > 0.5:
                failures.append(f"{label}: {key} edges differ: {sorted(set(round(v, 1) for v in values))}")
        for key in ("statusFits", "pingFits", "upFits"):
            if not all(r[key] for r in rows):
                failures.append(f"{label}: text overflows its column ({key})")
        if data["head"]:
            first = rows[0]
            for head_key, row_key in (("status", "status"), ("ping", "ping"), ("up", "up")):
                if abs(data["head"][head_key] - first[row_key]) > 0.5:
                    failures.append(f"{label}: list header {head_key} edge {data['head'][head_key]:.1f} "
                                    f"!= row {first[row_key]:.1f}")
        print(f"  {label}: {len(rows)} rows, status left {rows[0]['status']:.1f}, ping right {rows[0]['ping']:.1f}, "
              f"uptime right {rows[0]['up']:.1f}, name column {min(r['nameWidth'] for r in rows):.0f}px"
              + (", header aligned" if data["head"] else ""))
        context.close()


REFRESH = """async () => {
    // Hide and show the page: public.js refreshes the live parts when it becomes visible again.
    const done = new Promise((resolve) => {
        const original = window.fetch;
        window.fetch = (...args) => original(...args).then((r) => { setTimeout(resolve, 300); return r; });
    });
    for (const hidden of [true, false]) {
        Object.defineProperty(document, 'hidden', {value: hidden, configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }
    await done;
}"""


def check_refresh(browser, panel: Panel, failures: list[str]) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 500}, color_scheme="dark")
    page = context.new_page()
    page.goto(panel.url + "/")
    settle(page)
    order = page.evaluate("""() => [...document.querySelectorAll(
        '.page-top, #live-summary, .sort-row, #live-list, .site-footer')].map(el => el.id || el.className)""")
    if order != ["page-top", "live-summary", "sort-row", "live-list", "site-footer fine"]:
        failures.append(f"page order is {order}")
    live = page.evaluate("""() => ({
        container: document.getElementById('live-summary').getAttribute('aria-live'),
        inner: document.querySelectorAll('#live-summary [aria-live]').length})""")
    if live != {"container": "polite", "inner": 0}:
        failures.append(f"aria-live should be only on #live-summary: {live}")

    sid = panel.ids["🇫🇮 Helsinki gRPC"]
    page.click('[data-tab="fastest"]')
    row = f'#servers > li[data-id="{sid}"]'
    page.click(f"{row} .row-main")
    with page.expect_response(lambda r: "range=7d" in r.url):
        page.click(f'{row} .chart-range [data-range="7d"]')
    page.evaluate(f"""() => {{
        document.querySelector('{row} .row-main').focus();
        window.scrollTo(0, 260);
        window.__summary = [...document.getElementById('live-summary').children];
        window.__list = document.querySelector('#servers');
    }}""")
    before = page.evaluate("window.scrollY")
    page.evaluate(REFRESH)
    state = page.evaluate(f"""() => ({{
        listReplaced: document.querySelector('#servers') !== window.__list,
        summaryKept: [...document.getElementById('live-summary').children].every((el, i) => el === window.__summary[i]),
        open: document.querySelector('{row} .row-main').getAttribute('aria-expanded') === 'true'
              && !document.getElementById('detail-{sid}').hidden,
        tab: document.querySelector('[data-tab].is-active').dataset.tab,
        range: document.querySelector('{row} .chart-range .is-active').dataset.range,
        chartDrawn: document.querySelector('{row} canvas').width > 0,
        focus: document.activeElement === document.querySelector('{row} .row-main'),
        scrollY: window.scrollY,
    }})""")
    expected = {"listReplaced": True, "summaryKept": True, "open": True, "tab": "fastest", "range": "7d",
                "chartDrawn": True, "focus": True, "scrollY": before}
    if state != expected:
        failures.append(f"refresh did not keep the view: {state} (expected {expected})")
    print(f"  refresh keeps open row, tab, chart range, scroll and focus: {state == expected}; "
          f"unchanged summary left in place: {state['summaryKept']}")

    page.evaluate("document.querySelector('#live-summary .banner').textContent = 'stale text'")
    page.evaluate(REFRESH)
    banner = page.evaluate("document.querySelector('#live-summary .banner').textContent")
    if banner == "stale text":
        failures.append("summary text changed on the server but was not replaced")
    print(f"  changed summary replaced: {banner != 'stale text'} ({banner!r})")
    context.close()


def check_flag_font(browser, panel: Panel, failures: list[str]) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(panel.url + "/")
    settle(page)
    native = not page.evaluate("document.documentElement.classList.contains('flags-polyfill')")
    print(f"  this browser draws flags {'natively' if native else 'with the bundled font (flags.js added the class)'}")
    context.close()

    def run(flags_js: str, font_status: int | None = None) -> dict:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        fonts, errors = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and FLAG_FONT not in
                msg.text and "404" not in msg.text else None)
        page.on("response", lambda r: fonts.append(r.status) if FLAG_FONT in r.url else None)
        page.route("**/js/flags.js*", lambda route: route.fulfill(content_type="text/javascript", body=flags_js))
        if font_status is not None:
            page.route(f"**/fonts/{FLAG_FONT}", lambda route: route.fulfill(status=font_status, body="missing"))
        page.goto(panel.url + "/")
        settle(page)
        state = page.evaluate("""() => ({
            check: document.fonts.check('16px "Twemoji Country Flags"', '\\u{1F1E9}\\u{1F1EA}'),
            face: [...document.fonts].filter(f => f.family.includes('Twemoji')).map(f => f.status),
            family: getComputedStyle(document.body).fontFamily,
            rows: document.querySelectorAll('#servers > li').length,
        })""")
        page.goto(panel.url + "/admin/login")
        settle(page)
        state["input"] = page.evaluate("getComputedStyle(document.getElementById('username')).fontFamily")
        context.close()
        return {**state, "requests": fonts, "errors": errors}

    forced = run("document.documentElement.classList.add('flags-polyfill');")
    if not (forced["requests"] and forced["requests"][0] == 200 and "loaded" in forced["face"] and forced["check"]):
        failures.append(f"font not loaded with .flags-polyfill: {forced}")
    if not forced["family"].startswith('"Twemoji Country Flags"') or not forced["input"].startswith('"Twemoji'):
        failures.append(f"font not first in font-family: body {forced['family']!r}, input {forced['input']!r}")
    print(f"  with .flags-polyfill: font requests {forced['requests']}, face {forced['face']}, "
          f"fonts.check {forced['check']}, inputs use it: {forced['input'].startswith(chr(34) + 'Twemoji')}")

    plain = run("/* native flags: no class */")
    if plain["requests"]:
        failures.append(f"font downloaded without .flags-polyfill: {plain['requests']}")
    print(f"  without the class: font requests {plain['requests'] or 'none'}")

    missing = run("document.documentElement.classList.add('flags-polyfill');", font_status=404)
    if missing["errors"] or not missing["rows"]:
        failures.append(f"page broken without the font file: {missing}")
    print(f"  font file missing (404): {missing['rows']} rows rendered, JS errors: {missing['errors'] or 'none'}")


# -------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="run layout and flag-font checks, write nothing")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is missing: pip install playwright && python -m playwright install chromium")
    logging.basicConfig(level=logging.WARNING)
    tmp = Path(tempfile.mkdtemp(prefix="v2uptime-shots-"))
    failures: list[str] = []
    try:
        with Panel(tmp) as panel, sync_playwright() as pw:
            browser = pw.chromium.launch()
            if args.check:
                print("Layout (longest values injected):")
                check_layout(browser, panel, failures)
                print("Refresh:")
                check_refresh(browser, panel, failures)
                print("Flag font:")
                check_flag_font(browser, panel, failures)
            else:
                print("Screenshots:")
                take_screenshots(browser, panel)
            browser.close()
    finally:
        from app.db import db

        if not db.is_closed():
            db.close()
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures))
        raise SystemExit(1)
    if args.check:
        print("All checks passed.")


if __name__ == "__main__":
    main()
