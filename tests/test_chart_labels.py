"""Y-axis numbers on the public chart are painted after the line."""

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]


def test_y_labels_are_drawn_after_the_line():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<!doctype html><html><head><style>"
            ":root{--surface:#fff;--muted:#667;--border:#ccc;--accent:#36c;--fs-xs:12px}"
            "canvas{width:400px;height:160px;display:block}"
            "</style></head><body><canvas id='c'></canvas></body></html>"
        )
        page.add_script_tag(path=str(ROOT / "app" / "static" / "js" / "chart.js"))
        result = page.evaluate(
            """() => {
                const calls = [];
                const props = {};
                const ctx = new Proxy({}, {
                    get(_target, prop) {
                        if (prop === "measureText") return (text) => ({width: String(text).length * 7});
                        return (...args) => {
                            if (prop === "fillText") calls.push(["fillText", String(args[0])]);
                            else if (prop === "stroke") calls.push(["stroke", props.lineWidth]);
                            else if (prop === "fillRect") calls.push(["fillRect"]);
                            else if (prop === "setTransform") calls.push(["setTransform"]);
                            else calls.push([String(prop)]);
                        };
                    },
                    set(_target, prop, value) { props[prop] = value; return true; },
                });
                HTMLCanvasElement.prototype.getContext = () => ctx;
                const start = Date.parse("2026-11-01T00:00:00Z");
                const bars = [];
                const points = [];
                for (let i = 0; i < 6; i++) {
                    const t = new Date(start + i * 60000).toISOString();
                    bars.push({start: t, end: new Date(start + (i + 1) * 60000).toISOString(), level: "up", uptime: 100});
                    points.push({t, latency: 100 + i * 40});
                }
                window.MiniChart.timeline(document.getElementById("c"), {bars, points, range: "1h"});
                const draws = [];
                let current = [];
                for (const call of calls) {
                    if (call[0] === "setTransform" && current.length) { draws.push(current); current = []; }
                    current.push(call);
                }
                if (current.length) draws.push(current);
                const problems = [];
                draws.forEach((draw, index) => {
                    let line = -1;
                    draw.forEach((call, at) => { if (call[0] === "stroke" && call[1] === 1.6) line = at; });
                    const labels = draw.map((call, at) => ({at, call})).filter((item) => item.call[0] === "fillText" && /^[0-9]+$/.test(item.call[1]));
                    const backs = draw.map((call, at) => at).filter((at) => draw[at][0] === "fillRect" && at > line);
                    if (line < 0 || !labels.length || labels.some((item) => item.at < line) || backs.length < labels.length)
                        problems.push({index, line, labels, backs: backs.length});
                });
                return {ok: problems.length === 0 && draws.length > 0, problems, draws: draws.length};
            }"""
        )
        browser.close()
    assert result["ok"], result
