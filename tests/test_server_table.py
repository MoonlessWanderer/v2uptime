"""The server table fits at 1280px, and the grip reorders rows from the keyboard."""

import socket
import threading
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")

from app.config import Config
from app.main import create_app
from app.models import Admin
from app.security import hash_password
from tests.conftest import make_server

LONG = "Long name " + "n" * 50


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def panel(tmp_path):
    port = _port()
    cfg = Config(host="127.0.0.1", port=port, db_path=tmp_path / "ui.db", secret_key="ui-secret",
                 xray_bin=tmp_path / "no-xray", log_dir=tmp_path / "logs", cookie_secure=False)
    app = create_app(cfg, background=False)
    make_server("Parked", port=24131, active=False)
    reality = make_server("Reality node", port=24132)
    reality.security = "reality"
    reality.save()
    make_server(LONG, port=24133)
    make_server("Plain", port=24134)
    Admin.create(username="admin", password_hash=hash_password("correct-horse"))
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("panel did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


FIT = """() => {
    const wrap = document.querySelector(".table-wrap");
    const wr = wrap.getBoundingClientRect();
    const outside = [];
    for (const row of document.querySelectorAll("#server-table tbody tr[data-id]")) {
        const btn = row.querySelector('button[aria-label="Delete"]');
        const box = btn.getBoundingClientRect();
        if (box.bottom <= 0 || box.top >= window.innerHeight || box.height === 0) continue;
        if (box.right > wr.right + 1) outside.push(Math.round(box.right - wr.right));
    }
    return {scroll: wrap.scrollWidth, client: wrap.clientWidth, outside};
}"""

STICKY = """() => {
    const wrap = document.querySelector(".table-wrap");
    const proto = document.querySelector("#server-table tbody tr td:nth-child(5)");
    const before = proto.getBoundingClientRect().left;
    wrap.scrollLeft = 240;
    const wr = wrap.getBoundingClientRect();
    const box = (sel) => document.querySelector("#server-table tbody " + sel).getBoundingClientRect();
    const check = box("td.col-check");
    const handle = box("td.col-handle");
    const name = box("td.col-name");
    return {
        overflow: wrap.scrollWidth > wrap.clientWidth,
        moved: Math.round(before - proto.getBoundingClientRect().left),
        check: Math.round(check.left - wr.left),
        handleGap: Math.round(handle.left - check.right),
        nameGap: Math.round(name.left - handle.right),
        bg: getComputedStyle(name && document.querySelector("#server-table tbody td.col-name")).backgroundColor,
        scrolled: wrap.scrollLeft,
    };
}"""


def _ids(page):
    return page.evaluate("() => [...document.querySelectorAll('#server-table tbody tr[data-id]')].map(tr => tr.dataset.id)")


def test_server_table_fits_and_reorders_from_the_keyboard(panel):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(panel + "/admin/login")
        page.fill("#username", "admin")
        page.fill("#password", "correct-horse")
        with page.expect_navigation():
            page.click("button[type=submit]")
        page.goto(panel + "/admin/servers")
        page.wait_for_selector("#server-table tbody tr[data-id]")
        assert len(LONG) == 60
        assert page.locator("[data-move]").count() == 0
        assert page.locator("td.col-status .pill-warn", has_text="Not loaded").count() >= 1
        assert page.locator("td.num", has_text="Not loaded").count() == 0
        chip = page.locator("#server-table .chip", has_text="VLESS · tcp · reality")
        assert chip.count() == 1 and "none" not in chip.inner_text()
        parked = page.locator("#server-table tbody tr", has_text="Parked")
        power = parked.locator("[data-toggle='is_active']")
        assert power.get_attribute("aria-pressed") == "false"
        assert "is-off" in (power.get_attribute("class") or "")
        assert parked.locator(".col-status").inner_text().strip().startswith("Inactive")
        assert page.locator("td.col-name a").filter(has_text=LONG).get_attribute("title") == LONG

        for width in (1280, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.locator("#server-table").evaluate("el => el.scrollIntoView({block: 'start'})")
            fit = page.evaluate(FIT)
            assert fit["scroll"] <= fit["client"] and fit["outside"] == [], fit

        page.set_viewport_size({"width": 800, "height": 900})
        page.locator("#server-table").evaluate("el => el.scrollIntoView({block: 'start'})")
        sticky = page.evaluate(STICKY)
        assert sticky["overflow"] and sticky["scrolled"] > 0, sticky
        assert sticky["moved"] > 80, sticky
        assert abs(sticky["check"]) <= 20, sticky
        assert abs(sticky["handleGap"]) <= 2 and abs(sticky["nameGap"]) <= 2, sticky
        assert sticky["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), sticky

        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#server-table").evaluate("el => el.scrollIntoView({block: 'start'})")
        before = _ids(page)
        label = page.locator(".grip").first.get_attribute("aria-label")
        posts = []
        page.on("response", lambda response: posts.append(response.status)
                if response.request.method == "POST" and response.url.endswith("/servers/reorder") else None)
        page.locator(".grip").first.focus()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(1500)
        after = _ids(page)
        assert posts == [200], posts
        assert after[2] == before[0] and after[0] == before[1] and after[1] == before[2]
        assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == label
        page.reload()
        page.wait_for_selector("#server-table tbody tr[data-id]")
        assert _ids(page) == after
        browser.close()
