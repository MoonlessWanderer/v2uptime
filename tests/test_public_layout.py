"""Public page layout in English and Persian."""

import re
import socket
import threading
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")

from app.config import Config
from app.main import create_app
from app.services.settings import settings
from tests.conftest import add_checks, make_server
from app.timeutil import now_ts


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
    server = make_server("Alpha", port=24011, public=True)
    add_checks(server, now_ts() - 3600, "o" * 12)
    http = uvicorn_server(app, port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        http.should_exit = True
        http.thread.join(timeout=15)


def uvicorn_server(app, port):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    server.thread = thread
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("panel did not start")


def _no_horizontal_scroll(page) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1


def _open_and_align(page) -> None:
    page.locator(".card-name").click()
    page.wait_for_selector(".timeline:not(.is-loading)")
    aligned = page.evaluate("""() => {
        const bars = document.querySelector('.range-bars').getBoundingClientRect();
        const canvas = document.querySelector('.timeline canvas').getBoundingClientRect();
        const timeline = document.querySelector('.timeline');
        const closed = document.querySelector('.card-bars');
        return {
            left: Math.abs(bars.left - canvas.left),
            right: Math.abs(bars.right - canvas.right),
            timeline: timeline.getAttribute('dir'),
            bars: closed.getAttribute('dir'),
        };
    }""")
    assert aligned["left"] <= 1 and aligned["right"] <= 1
    assert aligned["timeline"] == "ltr" and aligned["bars"] == "ltr"
    stats = page.evaluate("""() => {
        const read = (name) => {
            const el = document.querySelector('[data-stat="' + name + '"]');
            const style = getComputedStyle(el);
            return {text: el.innerText.trim(), bidi: style.unicodeBidi, direction: style.direction};
        };
        return {ping: read('ping'), uptime: read('uptime'), incidents: read('incidents')};
    }""")
    for name in ("ping", "uptime", "incidents"):
        assert stats[name]["bidi"] == "isolate" and stats[name]["direction"] == "ltr"
    assert re.fullmatch(r"\d+ ms", stats["ping"]["text"])


def _hour_tooltip_matches_the_closed_card(page) -> None:
    """The open 1h tip is the same minute as the newest closed-card check, not the next minute."""
    box = page.locator(".timeline").bounding_box()
    page.mouse.move(box["x"] + box["width"] - 6, box["y"] + 36)
    page.wait_for_selector("#tooltip:not([hidden])")
    compared = page.evaluate("""() => {
        const cells = [...document.querySelectorAll('.card-bars .b')].filter((cell) => cell.dataset.t);
        const clock = window.UptimeTime.formatTime(cells[cells.length - 1].dataset.t);
        const tip = document.getElementById('tooltip').textContent;
        return {tip, clock};
    }""")
    assert compared["clock"] in compared["tip"]
    assert "–" not in compared["tip"].split("·")[0]
    page.locator('.range-seg [data-range="24h"]').click()
    page.wait_for_selector(".timeline:not(.is-loading)")
    box = page.locator(".timeline").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 36)
    page.wait_for_selector("#tooltip:not([hidden])")
    assert "–" in page.locator("#tooltip").inner_text()


def test_public_layout_in_both_languages(panel):
    from playwright.sync_api import sync_playwright

    settings.set("test_location", "Tehran")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        for lang in ("en", "fa"):
            settings.set("public_language", lang)
            for width in (360, 390, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                page.goto(panel + "/")
                page.wait_for_selector(".card")
                assert page.locator("html").get_attribute("lang") == lang
                _no_horizontal_scroll(page)
                html = page.content()
                assert page.locator("footer").count() == 0
                assert "Test location" not in html and "محل تست" not in html
                assert "Tehran" not in page.locator(".check-line").inner_text()
                title = page.locator(".check-line time").get_attribute("title")
                assert title and re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC[+-]\d{2}:\d{2}$", title)
                if lang == "fa":
                    edges = page.evaluate("""() => {
                        const card = document.querySelector('.card').getBoundingClientRect();
                        const ping = document.querySelector('.card-ping').getBoundingClientRect();
                        const name = document.querySelector('.card-name').getBoundingClientRect();
                        return {ping: ping.left - card.left, name: card.right - name.right};
                    }""")
                    assert edges["ping"] < 20
                    assert edges["name"] < 20
                _open_and_align(page)
                if lang == "en" and width == 1280:
                    _hour_tooltip_matches_the_closed_card(page)
                page.locator(".card-name").click()  # close before the next width

        # A finger on the left of the Persian chart selects the oldest cell.
        page.set_viewport_size({"width": 390, "height": 800})
        settings.set("public_language", "fa")
        page.goto(panel + "/")
        page.locator(".card-name").click()
        page.wait_for_selector(".timeline:not(.is-loading)")
        box = page.locator(".timeline").bounding_box()
        page.dispatch_event(".timeline", "pointerdown", {
            "pointerType": "touch", "clientX": box["x"] + 4, "clientY": box["y"] + 10, "bubbles": True,
        })
        assert page.evaluate("() => document.querySelector('.range-bars .b.is-hover') === document.querySelector('.range-bars .b')")
        count = page.locator(".range-bars .b").count()
        page.dispatch_event(".timeline", "pointerdown", {
            "pointerType": "touch", "clientX": box["x"] + box["width"] - 4,
            "clientY": box["y"] + 10, "bubbles": True,
        })
        assert page.evaluate(
            "() => { const cells = document.querySelectorAll('.range-bars .b');"
            " return cells[cells.length - 1].classList.contains('is-hover'); }"
        )
        assert count > 2

        page.set_viewport_size({"width": 1280, "height": 800})
        settings.set("public_language", "en")
        page.context.clear_cookies()
        page.goto(panel + "/")
        assert page.locator("html").get_attribute("lang") == "en"
        settings.set("public_language", "fa")
        page.goto(panel + "/")
        page.wait_for_selector("html[lang=fa]")
        page.reload()
        assert page.locator("html").get_attribute("lang") == "fa"
        fragment = page.evaluate("async () => (await fetch('/partials/live?range=24h')).text()")
        assert "جدید" in fragment or "آنلاین" in fragment or "آپتایم" in fragment
        settings.set("public_language", "en")
        page.goto(panel + "/")
        page.wait_for_selector("html[lang=en]")
        browser.close()
