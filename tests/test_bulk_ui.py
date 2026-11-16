"""The server list can hide every visible row, and those servers leave the public page."""

import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

pytest.importorskip("playwright.sync_api")

from app.config import Config
from app.main import create_app
from app.models import Admin
from app.security import hash_password
from tests.conftest import make_server


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
    for name, socks in (("Alpha", 24001), ("Beta", 24002), ("Gamma", 24003)):
        make_server(name, port=socks, public=True)
    Admin.create(username="admin", password_hash=hash_password("correct-horse"))
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


def test_select_all_makes_the_visible_servers_hidden(panel):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(panel + "/admin/login")
        page.fill("#username", "admin")
        page.fill("#password", "correct-horse")
        with page.expect_navigation():
            page.click("button[type=submit]")
        page.goto(panel + "/admin/servers")
        boxes = page.locator("tbody .row-check")
        assert boxes.count() == 3
        box = boxes.first.bounding_box()
        assert box is not None and box["width"] >= 20 and box["height"] >= 20
        page.locator("#check-all").check()
        assert page.locator("#bulk-count").inner_text() == "3 selected"
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("[data-bulk=hidden]").click()
        page.wait_for_selector('use[href="#i-eye-off"]')
        assert page.locator('use[href="#i-eye-off"]').count() == 3
        public = browser.new_page()
        public.goto(panel + "/")
        text = public.inner_text("body")
        assert "Alpha" not in text and "Beta" not in text and "Gamma" not in text
        browser.close()
