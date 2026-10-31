"""Rebuild the README screenshots (docs/screenshots/*.jpg) from sample data.

    python scripts/screenshots.py            # write the screenshots, including the Persian status page
    python scripts/screenshots.py --check    # layout, card, touch, refresh and flag-font checks (about 2 minutes)

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
VAZIR = ROOT / "app" / "static" / "fonts" / "Vazirmatn-Variable.woff2"
# Same files install.sh downloads. The first that answers is enough.
VAZIR_URLS = (
    "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2",
    "https://raw.githubusercontent.com/rastikerdar/vazirmatn/v33.003/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2",
)

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
    seed_history(now, ids, rng)
    from app.security import hash_password

    password = secrets.token_urlsafe(18)
    Admin.create(username="admin", password_hash=hash_password(password))
    ids["__password__"] = password  # type: ignore[assignment]
    return ids


PAST_OUTAGES = {  # name: [(hours ago, minutes)] before the last 24 hours, so 7d and 30d differ from 24h
    "Amsterdam WS 🇳🇱": [(5 * 24 + 7, 9), (19 * 24 + 2, 21)],
    "🇫🇮 Helsinki gRPC": [(3 * 24 + 15, 35), (12 * 24 + 4, 12), (26 * 24 + 20, 48)],
    "🇮🇷➡️🇩🇪 Tunnel": [(9 * 24 + 1, 17)],
}


def seed_history(now: int, ids: dict[str, int], rng: random.Random) -> None:
    """Hourly rows for days 2–30, as the aggregator would have written them, plus their incidents."""
    from app.db import db
    from app.models import HourlyStat, Incident
    from app.services.aggregator import percentile
    from app.services.settings import settings
    from app.timeutil import hour_floor

    end = hour_floor(now - 24 * 3600)  # the raw checks cover the last 24 hours
    rows = []
    for name, _link, ping, _connect in SERVERS:
        if name == "🇬🇧 London":
            continue
        outages = [(now - h * 3600, now - h * 3600 + m * 60) for h, m in PAST_OUTAGES.get(name, [])]
        for hour in range(hour_floor(now - 30 * 86400), end, 3600):
            fail = sum(max(0, min(hour + 3600, e) - max(hour, s)) // 60 for s, e in outages)
            slow = 1.4 if rng.random() < 0.025 else 1.0  # an occasional slow hour
            lat = [max(20, int(slow * ping * (1 + 0.12 * math.sin((hour + k * 60) / 5400)) + rng.gauss(0, ping * 0.07)))
                   for k in range(60 - fail)]
            rows.append({
                "server": ids[name], "hour": hour, "total": 60, "success": len(lat), "fail": fail, "unknown": 0,
                "latency_count": len(lat), "latency_sum": float(sum(lat)), "latency_sq_sum": float(sum(x * x for x in lat)),
                "avg_latency": sum(lat) / len(lat) if lat else None, "min_latency": min(lat) if lat else None,
                "max_latency": max(lat) if lat else None, "p95_latency": percentile(lat, 95),
            })
        for start, stop in outages:
            Incident.create(server=ids[name], started_at=start, ended_at=stop, duration_seconds=stop - start,
                            error_type="timeout")
    with db.atomic():
        for start in range(0, len(rows), 500):
            HourlyStat.insert_many(rows[start:start + 500]).execute()
    settings.set("agg_until", end, validated=True)


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


def ensure_vazirmatn() -> None:
    """Download Vazirmatn when the repo does not have it, so screenshots are not fallback text."""
    if VAZIR.is_file() and VAZIR.stat().st_size > 0:
        return
    VAZIR.parent.mkdir(parents=True, exist_ok=True)
    for url in VAZIR_URLS:
        try:
            with urlopen(url, timeout=30) as response:
                data = response.read()
        except OSError:
            continue
        if data.startswith(b"wOF2"):
            VAZIR.write_bytes(data)
            return
    raise SystemExit(
        "Could not download Vazirmatn to app/static/fonts/Vazirmatn-Variable.woff2. "
        "Screenshots stop here instead of using a fallback font. "
        "The URLs are the ones install.sh uses."
    )


def settle(page) -> None:
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    if VAZIR.is_file():
        loaded = page.evaluate(
            """() => [...document.fonts].some((f) => f.family.replace(/"/g, "") === "Vazirmatn" && f.status === "loaded")"""
        )
        if not loaded:
            raise SystemExit("Vazirmatn did not load. Refusing to continue with a fallback font.")
    page.wait_for_timeout(300)


def take_screenshots(browser, panel: Panel) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    desktop = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    page = desktop.new_page()
    page.goto(panel.url + "/")
    settle(page)
    save_jpeg(page, OUT_DIR / "status-page.jpg", full_page=True)

    # A full-page capture resizes the viewport, and a resize hides the tooltip: use a tall viewport instead.
    page.set_viewport_size({"width": 1280, "height": 1260})
    card = open_card(page, panel.ids["🇫🇮 Helsinki gRPC"])
    worst = card.evaluate("""li => {
        const bars = li.querySelector('.timeline')._data.bars;
        let pick = 0;
        bars.forEach((b, i) => { if (b.uptime !== null && b.uptime < (bars[pick].uptime ?? 101)) pick = i; });
        return [pick, bars.length];
    }""")
    box = card.locator(".timeline").bounding_box()
    page.mouse.move(box["x"] + (worst[0] + 0.5) / worst[1] * box["width"], box["y"] + 70)  # cursor line + tooltip
    page.wait_for_timeout(200)
    save_jpeg(page, OUT_DIR / "server-detail.jpg")
    desktop.close()

    mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                 has_touch=True, color_scheme="dark")
    page = mobile.new_page()
    page.goto(panel.url + "/")
    settle(page)
    save_jpeg(page, OUT_DIR / "mobile.jpg", full_page=True)
    mobile.close()

    persian = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    page = persian.new_page()
    from app.services.settings import settings
    settings.set("public_language", "fa")
    page.goto(panel.url + "/")
    settle(page)
    open_card(page, panel.ids["🇩🇪 Germany | Reality"])
    save_jpeg(page, OUT_DIR / "status-page-fa.jpg", full_page=True)
    persian.close()

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

def open_card(page, server_id: int):
    """Open a card by clicking its status line (line 3) and wait for its history."""
    card = page.locator(f'#servers > li[data-id="{server_id}"]')
    with page.expect_response(lambda r: "/history" in r.url):
        card.locator(".card-sub").click()
    settle(page)
    return card


CARDS = """() => {
    const box = (el) => el.getBoundingClientRect();
    const text = (el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect(); };
    const cards = [...document.querySelectorAll('#servers > li')];
    return {
        scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
        cards: cards.map((li) => ({
            top: box(li).top, bottom: box(li).bottom, height: box(li).height,
            ping: text(li.querySelector('.card-ping')).right,
            nameFits: box(li.querySelector('.card-name')).right <= box(li.querySelector('.card-ping')).left + 0.5,
        })),
    };
}"""

LONG_NAME = """() => {
    document.querySelector('#servers > li .card-name bdi').textContent =
        '🇩🇪 A very long server name that has to be cut short on a phone | Reality';
}"""

OPEN_STATE = """(li) => {
    const box = (el) => el.getBoundingClientRect();
    const bars = li.querySelector('.range-bars'), canvas = li.querySelector('.timeline canvas');
    return {
        open: li.classList.contains('is-open'),
        closedBarHidden: getComputedStyle(li.querySelector('.card-bars')).display === 'none',
        range: li.querySelector('.range-seg [aria-pressed="true"]').dataset.range,
        cells: bars.children.length,
        stats: [...li.querySelectorAll('[data-stat]')].map((d) => d.textContent).join(' | '),
        chart: canvas.toDataURL().length + ':' + canvas.toDataURL().slice(-64),
        edges: [box(bars).left - box(canvas).left, box(bars).right - box(canvas).right],
        label: canvas.getAttribute('aria-label'),
    };
}"""


def check_layout(browser, panel: Panel, failures: list[str]) -> None:
    for width in (360, 390, 1280):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark",
                                      is_mobile=width < 700, has_touch=width < 700)
        page = context.new_page()
        page.goto(panel.url + "/")
        settle(page)
        page.evaluate(LONG_NAME)
        data = page.evaluate(CARDS)
        cards, label = data["cards"], f"{width}px"
        # A phone widens its layout viewport to fit wide content, so compare with the requested width too.
        if data["scrollWidth"] > width or data["innerWidth"] != width:
            failures.append(f"{label}: horizontal scroll (page {data['scrollWidth']}px, viewport {data['innerWidth']}px)")
        gaps = [b["top"] - a["bottom"] for a, b in zip(cards, cards[1:])]
        if min(gaps) < 8:
            failures.append(f"{label}: cards closer than 8px ({min(gaps):.1f})")
        if max(c["height"] for c in cards) >= 115:
            failures.append(f"{label}: a closed card is {max(c['height'] for c in cards):.0f}px tall")
        pings = [c["ping"] for c in cards]
        if max(pings) - min(pings) > 0.5:
            failures.append(f"{label}: ping right edges differ: {sorted(set(round(p, 1) for p in pings))}")
        if not all(c["nameFits"] for c in cards):
            failures.append(f"{label}: a name runs into the ping")
        print(f"  {label}: {len(cards)} cards, height {min(c['height'] for c in cards):.0f}-"
              f"{max(c['height'] for c in cards):.0f}px, gap {min(gaps):.0f}px, ping right {pings[0]:.1f}")
        context.close()


def check_open_card(browser, panel: Panel, failures: list[str]) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.goto(panel.url + "/")
    settle(page)
    card = open_card(page, panel.ids["Amsterdam WS 🇳🇱"])
    day = card.evaluate(OPEN_STATE)
    if not (day["open"] and day["closedBarHidden"] and day["range"] == "1h" and day["cells"] == 60):
        failures.append(f"open card (1h): {day}")
    if max(abs(e) for e in day["edges"]) > 1:
        failures.append(f"bar and chart edges differ by {day['edges']}")
    if not day["label"].startswith("Ping over the last 1 hour, average "):
        failures.append(f"chart label: {day['label']!r}")
    print(f"  opened by its status line; 1h: {day['cells']} cells, {day['stats']}, "
          f"bar/chart edge offset {day['edges'][0]:.2f}/{day['edges'][1]:.2f}px")
    seen = {day["stats"]: "1h"}
    for range_key, cells in (("24h", 48), ("7d", 42), ("30d", 30)):
        with page.expect_response(lambda r, k=range_key: f"range={k}" in r.url):
            card.locator(f'.range-seg [data-range="{range_key}"]').click()
        settle(page)
        state = card.evaluate(OPEN_STATE)
        if state["range"] != range_key or state["cells"] != cells:
            failures.append(f"{range_key}: {state}")
        if state["stats"] in seen or state["chart"] == day["chart"]:
            failures.append(f"{range_key}: numbers or chart did not change ({state['stats']})")
        if max(abs(e) for e in state["edges"]) > 1:
            failures.append(f"{range_key}: bar and chart edges differ by {state['edges']}")
        seen[state["stats"]] = range_key
        print(f"  {range_key}: {state['cells']} cells, {state['stats']}")
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "/history" in r.url else None)
    card.locator('.range-seg [data-range="1h"]').click()
    page.wait_for_timeout(300)
    if requests:
        failures.append(f"switching back to a cached range sent {requests}")
    context.close()


def touch(cdp, kind: str, x: float = 0, y: float = 0) -> None:
    points = [] if kind == "touchEnd" else [{"x": x, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def check_touch(browser, panel: Panel, failures: list[str]) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True,
                                  has_touch=True)
    page = context.new_page()
    page.goto(panel.url + "/")
    settle(page)
    cdp = context.new_cdp_session(page)
    card = page.locator(f'#servers > li[data-id="{panel.ids["🇫🇮 Helsinki gRPC"]}"]')
    with page.expect_response(lambda r: "/history" in r.url):
        box = card.locator(".card-sub").bounding_box()
        page.touchscreen.tap(box["x"] + 40, box["y"] + box["height"] / 2)
    settle(page)
    chart = card.locator(".timeline .chart").bounding_box()
    y = chart["y"] + chart["height"] / 2
    positions = []
    touch(cdp, "touchStart", chart["x"] + chart["width"] * 0.2, y)
    for share in (0.3, 0.5, 0.7):
        touch(cdp, "touchMove", chart["x"] + chart["width"] * share, y)
        page.wait_for_timeout(80)
        positions.append(page.evaluate(f"""() => {{
            const c = document.querySelector('#servers > li[data-id="{panel.ids["🇫🇮 Helsinki gRPC"]}"] .cursor');
            return c.hidden ? null : c.style.left;
        }}"""))
    tip = page.evaluate("document.getElementById('tooltip').hidden ? '' : document.getElementById('tooltip').textContent")
    touch(cdp, "touchEnd")
    page.wait_for_timeout(150)
    still_open = card.evaluate("li => li.classList.contains('is-open')")
    if None in positions or len(set(positions)) != 3 or not tip or not still_open:
        failures.append(f"touch drag on the chart: cursor {positions}, tooltip {tip!r}, open {still_open}")
    print(f"  drag on chart: cursor at {positions}, tooltip {tip!r}, card still open: {still_open}")

    other = page.locator(f'#servers > li[data-id="{panel.ids["🇩🇪 Germany | Reality"]}"]')
    bar = other.locator(".card-bars").bounding_box()
    page.touchscreen.tap(bar["x"] + bar["width"] * 0.8, bar["y"] + bar["height"] / 2)
    page.wait_for_timeout(150)
    tip = page.evaluate("document.getElementById('tooltip').hidden ? '' : document.getElementById('tooltip').textContent")
    opened = other.evaluate("li => li.classList.contains('is-open')")
    if not tip or opened:
        failures.append(f"tap on a closed bar: tooltip {tip!r}, card opened {opened}")
    print(f"  tap on closed bar: tooltip {tip!r}, card opened: {opened}")
    context.close()


def check_refresh(browser, panel: Panel, failures: list[str]) -> None:
    """Waits for a real refresh (60s timer) with a card open on 7d."""
    context = browser.new_context(viewport={"width": 1280, "height": 600}, color_scheme="dark")
    page = context.new_page()
    page.goto(panel.url + "/")
    settle(page)
    order = page.evaluate("""() => [...document.querySelectorAll(
        '.page-top, #live-summary, .sort-row, #live-list, .site-footer')].map(el => el.id || el.className)""")
    if order != ["page-top", "live-summary", "sort-row", "live-list", "site-footer fine"]:
        failures.append(f"page order is {order}")
    live = page.evaluate("""() => ({
        container: document.getElementById('live-summary').getAttribute('aria-live'),
        inner: document.querySelectorAll('#live-summary [aria-live]').length,
        banner: document.querySelectorAll('.banner').length,
        text: document.querySelector('#live-summary .sr-only').textContent})""")
    if (live["container"], live["inner"], live["banner"]) != ("polite", 0, 0):
        failures.append(f"live summary: {live}")
    print(f"  screen-reader summary: {live['text']!r}")

    sid = panel.ids["Amsterdam WS 🇳🇱"]
    page.click('[data-tab="fastest"]')
    card = open_card(page, sid)
    with page.expect_response(lambda r: "range=7d" in r.url):
        card.locator('.range-seg [data-range="7d"]').click()
    settle(page)
    page.evaluate(f"""() => {{
        document.querySelector('#servers > li[data-id="{sid}"] .range-seg [data-range="7d"]').focus();
        window.scrollTo(0, 200);
        window.__summary = [...document.getElementById('live-summary').children];
        window.__list = document.getElementById('servers');
    }}""")
    before = page.evaluate("window.scrollY")
    page.wait_for_timeout(66_000)
    settle(page)
    state = page.evaluate(f"""() => {{
        const li = document.querySelector('#servers > li[data-id="{sid}"]');
        return {{
            listReplaced: document.getElementById('servers') !== window.__list,
            summaryKept: [...document.getElementById('live-summary').children].every((el, i) => el === window.__summary[i]),
            open: li.classList.contains('is-open') && !li.querySelector('.card-detail').hidden,
            range: li.querySelector('.range-seg [aria-pressed="true"]').dataset.range,
            cells: li.querySelectorAll('.range-bars .b').length,
            tab: document.querySelector('[data-tab].is-active').dataset.tab,
            focus: document.activeElement === li.querySelector('.range-seg [data-range="7d"]'),
            scrollY: window.scrollY,
        }};
    }}""")
    expected = {"listReplaced": True, "summaryKept": True, "open": True, "range": "7d", "cells": 42,
                "tab": "fastest", "focus": True, "scrollY": before}
    if state != expected:
        failures.append(f"after the 60s refresh: {state} (expected {expected})")
    print(f"  after a real refresh (66s): {state}")
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
                print("Closed cards (a long name injected):")
                check_layout(browser, panel, failures)
                print("Open card:")
                check_open_card(browser, panel, failures)
                print("Touch:")
                check_touch(browser, panel, failures)
                print("Refresh:")
                check_refresh(browser, panel, failures)
                print("Flag font:")
                check_flag_font(browser, panel, failures)
            else:
                print("Screenshots:")
                ensure_vazirmatn()
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
