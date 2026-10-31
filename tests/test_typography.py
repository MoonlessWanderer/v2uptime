"""One font, two weights, and the fixed size scale on the public page and in the admin panel."""

import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from app.config import Config
from app.main import create_app
from app.models import Admin
from app.security import hash_password
from app.timeutil import now_ts
from tests.conftest import add_checks, make_server

ROOT = Path(__file__).resolve().parents[1]
VAZIR = ROOT / "app" / "static" / "fonts" / "Vazirmatn-Variable.woff2"
SCALE = {"12px", "13px", "15px", "17px", "20px"}
WEIGHTS = {"400", "600"}

FAMILIES = """() => {
    document.documentElement.classList.remove("flags-polyfill");
    const family = (el) => getComputedStyle(el).fontFamily.replace(/"/g, "");
    const tooltip = document.getElementById("tooltip");
    const nodes = {
        name: document.querySelector(".card-name"),
        ping: document.querySelector(".card-ping"),
        state: document.querySelector(".card-state"),
        uptime: document.querySelector('[data-stat="uptime"]'),
        pingStat: document.querySelector('[data-stat="ping"]'),
        incidents: document.querySelector('[data-stat="incidents"]'),
        footer: document.querySelector("footer"),
        range: document.querySelector(".range-seg .seg"),
        tooltip: tooltip,
    };
    const families = {};
    for (const [key, el] of Object.entries(nodes)) families[key] = family(el);
    const canvas = document.querySelector(".timeline canvas");
    return {
        families,
        body: getComputedStyle(document.body).fontFamily,
        chart: canvas && canvas._chart ? canvas._chart.fontFamily : null,
        loaded: [...document.fonts].some((f) => f.family.replace(/"/g, "") === "Vazirmatn" && f.status === "loaded"),
    };
}"""

SCAN = """() => {
    const weights = new Set();
    const sizes = new Set();
    const bad = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let el;
    while ((el = walker.nextNode())) {
        if (el.closest("svg")) continue;
        if (el.closest("[hidden]")) continue;
        const style = getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const text = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
        if (!text) continue;
        const box = el.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) continue;
        let weight = style.fontWeight;
        if (weight === "normal") weight = "400";
        if (weight === "bold") weight = "700";
        const mono = el.matches("code, .mono, .log-box") || !!el.closest("code, .mono, .log-box");
        weights.add(weight);
        if (!mono) sizes.add(style.fontSize);
        const sizeOk = mono || ["12px", "13px", "15px", "17px", "20px"].includes(style.fontSize);
        if (!["400", "600"].includes(weight) || !sizeOk) {
            bad.push((el.className && el.className.toString()) || el.tagName);
            bad.push(weight + " " + style.fontSize + " " + el.innerText.slice(0, 40));
        }
    }
    return {weights: [...weights], sizes: [...sizes], bad};
}"""

MARKS = """() => {
    const read = (sel) => {
        const el = document.querySelector(sel);
        const style = getComputedStyle(el);
        return {color: style.color, weight: style.fontWeight};
    };
    return {ping: read(".card-ping"), up: read(".card-up")};
}"""


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
    server = make_server("Alpha", port=24021, public=True)
    add_checks(server, now_ts() - 3600, "o" * 12)
    Admin.create(username="admin", password_hash=hash_password("correct-horse"))
    import uvicorn

    http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=http.run, daemon=True)
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
        http.should_exit = True
        thread.join(timeout=15)


def _assert_scale(page) -> None:
    found = page.evaluate(SCAN)
    assert not found["bad"], found["bad"]
    assert set(found["weights"]) <= WEIGHTS
    assert set(found["sizes"]) <= SCALE


def _open(page) -> None:
    page.locator(".card-name").click()
    page.wait_for_selector(".timeline:not(.is-loading)")
    box = page.locator(".timeline").bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + 30)
    page.wait_for_selector("#tooltip:not([hidden])")


def test_type_is_shared_across_languages_and_the_admin_panel(panel):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for lang in ("en", "fa"):
            page.goto(panel + "/?lang=" + lang)
            page.wait_for_selector(".card")
            page.evaluate("() => document.documentElement.classList.remove('flags-polyfill')")
            _open(page)
            info = page.evaluate(FAMILIES)
            families = set(info["families"].values())
            assert len(families) == 1, info["families"]
            assert next(iter(families)).startswith("Vazirmatn")
            assert info["chart"] == info["body"]
            assert info["chart"].replace('"', "").startswith("Vazirmatn")
            if VAZIR.is_file():
                assert info["loaded"]
            _assert_scale(page)
            marks = page.evaluate(MARKS)
            page.locator('[data-tab="fastest"]').click()
            assert page.evaluate(MARKS) == marks
            page.locator('[data-tab="stable"]').click()
            assert page.evaluate(MARKS) == marks
            page.locator('[data-tab="all"]').click()
            assert page.evaluate(MARKS) == marks

        page.goto(panel + "/admin/login")
        page.fill("#username", "admin")
        page.fill("#password", "correct-horse")
        with page.expect_navigation():
            page.click("button[type=submit]")
        page.goto(panel + "/admin/")
        page.wait_for_selector("h1")
        page.evaluate("() => document.documentElement.classList.remove('flags-polyfill')")
        body = page.evaluate("() => getComputedStyle(document.body).fontFamily.replace(/\"/g, '')")
        assert body.startswith("Vazirmatn")
        if VAZIR.is_file():
            loaded = page.evaluate(
                """() => [...document.fonts].some((f) => f.family.replace(/"/g, "") === "Vazirmatn" && f.status === "loaded")"""
            )
            assert loaded
        _assert_scale(page)
        browser.close()
