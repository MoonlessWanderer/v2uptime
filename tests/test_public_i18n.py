"""The public page is English or Persian. The admin panel, API, bot, and CLI stay English."""

import json
import re
import shutil
import subprocess

import pytest

from app.public_i18n import EN, FA, summary_sentence
from app.services.settings import settings
from app.timeutil import fmt_ago, fmt_duration

from tests.conftest import make_server
from tests.test_web import app_factory, client, csrf_from, login  # noqa: F401

ARABIC = re.compile(r"[\u0600-\u06FF]")
PERSIAN_DIGITS = re.compile(r"[۰-۹]")


def _without_lang_switch(html: str) -> str:
    return re.sub(r'<div class="lang-switch".*?</div>', "", html, count=1, flags=re.S)


def test_every_english_key_exists_in_persian():
    missing = sorted(set(EN) - set(FA))
    assert missing == []
    assert not any(PERSIAN_DIGITS.search(value) for value in FA.values())


def test_english_summary_sentences_are_unchanged():
    assert summary_sentence(1, 1, 0, 0) == "All 1 server is online"
    assert summary_sentence(3, 3, 0, 0) == "All 3 servers are online"
    assert summary_sentence(5, 3, 1, 1) == "1 of 5 servers is down, 1 degraded"
    assert summary_sentence(5, 2, 2, 0) == "2 of 5 servers are down"
    assert summary_sentence(5, 3, 0, 1) == "1 of 5 servers is degraded"
    assert summary_sentence(4, 1, 0, 0) == "1 of 4 servers is online"


def test_persian_duration_and_ago_keep_the_same_units():
    assert fmt_duration(45, lang="fa") == "45 ثانیه"
    assert fmt_duration(47 * 60 + 2, coarse=True, lang="fa") == "47 دقیقه"
    assert fmt_duration(8130, lang="fa") == "2 ساعت و 15 دقیقه"
    assert fmt_duration(45, lang="en") == "45s"
    assert fmt_ago(1_000, 1_000 + 30, lang="fa") == "همین الان"
    assert fmt_ago(1_000, 1_000 + 5 * 60, lang="fa") == "5 دقیقه پیش"
    assert fmt_ago(1_000, 1_000 + 23 * 3600, lang="fa") == "23 ساعت پیش"
    assert FA["axis_24h"] == "24 ساعت پیش"
    assert fmt_ago(1_000, 1_000 + 5 * 60, lang="en") == "5m ago"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_duration_matches_python():
    catalog = {key: FA[key] for key in ("dur_d", "dur_h", "dur_m", "dur_s", "dur_join", "ago_just", "ago_m", "ago_h", "ago_d")}
    script = r"""
const fs = require("fs"), vm = require("vm");
const catalog = JSON.parse(process.argv[1]);
const sandbox = {window: {}, setInterval() {}, document: {
  readyState: "complete", documentElement: {lang: "fa"}, body: null,
  getElementById: (id) => id === "i18n" ? {textContent: JSON.stringify(catalog)} : null,
  querySelectorAll: () => [], addEventListener() {}
}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync("app/static/js/time.js", "utf8"), sandbox);
const T = sandbox.window.UptimeTime;
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(cases.map((c) => c.ago
  ? T.formatAgo(c.value, c.now, "fa")
  : T.formatDuration(c.seconds, c.coarse, "fa"))));
"""
    cases = [
        {"seconds": 45, "coarse": False},
        {"seconds": 47 * 60 + 2, "coarse": True},
        {"seconds": 8130, "coarse": False},
        {"ago": True, "value": 1000, "now": 1300 * 1000},
    ]
    # formatAgo treats a number as unix seconds and nowMs as milliseconds.
    cases[3]["now"] = (1000 + 5 * 60) * 1000
    proc = subprocess.run(
        ["node", "-e", script, json.dumps(catalog), json.dumps(cases)],
        encoding="utf-8",
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got[0] == fmt_duration(45, lang="fa")
    assert got[1] == fmt_duration(47 * 60 + 2, coarse=True, lang="fa")
    assert got[2] == fmt_duration(8130, lang="fa")
    assert got[3] == "5 دقیقه پیش"


def test_query_cookie_and_default_choose_the_language(client):
    make_server("Alpha", port=22001)
    settings.set("public_language", "fa")
    default = client.get("/")
    assert 'lang="fa"' in default.text and 'dir="rtl"' in default.text
    assert "آنلاین" in default.text or "جدید" in default.text
    assert not PERSIAN_DIGITS.search(default.text)

    chosen = client.get("/?lang=en")
    assert 'lang="en"' in chosen.text and 'dir="ltr"' in chosen.text
    assert ARABIC.search(_without_lang_switch(chosen.text)) is None
    cookie = chosen.headers["set-cookie"]
    assert "lang=en" in cookie and "samesite=lax" in cookie.lower() and "max-age=31536000" in cookie.lower()

    # The cookie now says en, which beats the panel default.
    remembered = client.get("/")
    assert 'lang="en"' in remembered.text

    # The URL beats the cookie.
    again = client.get("/?lang=fa")
    assert 'lang="fa"' in again.text
    live = client.get("/partials/live")
    assert "جدید" in live.text or "آنلاین" in live.text
    assert PERSIAN_DIGITS.search(live.text) is None


def test_persian_footer_and_stat_values_stay_isolated(client):
    make_server("Alpha", port=24112, public=True)
    page = client.get("/?lang=fa").text
    assert "زمان‌ها به وقت" in page
    match = re.search(r'<bdi dir="ltr">([^<]+)</bdi>', page)
    assert match, page
    zone = match.group(1)
    assert zone.startswith("UTC+") and "(" in zone and zone.endswith(")")
    assert 'class="num" data-stat="ping"' in page
    assert 'class="num" data-stat="uptime"' in page
    assert 'class="num" data-stat="incidents"' in page


def test_optional_persian_text_falls_back_when_empty(client):
    make_server("Alpha", port=24111, public=True)
    settings.set("site_title_fa", "وضعیت سرورها")
    settings.set("site_description_fa", "")
    settings.set("test_location_fa", "تهران")
    settings.set("test_location", "Office")
    persian = client.get("/?lang=fa").text
    assert "وضعیت سرورها" in persian and "تهران" in persian
    assert "Real-time connection quality of our servers" in persian
    english = client.get("/?lang=en").text
    assert "وضعیت سرورها" not in english and "تهران" not in english
    assert "Office" in english

    settings.set("site_title_fa", "")
    settings.set("test_location_fa", "")
    fallback = client.get("/?lang=fa").text
    assert "Server Status" in fallback and "Office" in fallback


def test_public_error_follows_the_language_and_admin_does_not(client):
    client.get("/?lang=fa")
    missing = client.get("/no-such-page")
    assert missing.status_code == 404
    assert 'lang="fa"' in missing.text and "صفحه پیدا نشد." in missing.text
    login(client)
    admin = client.get("/admin/no-such-page")
    assert "Page not found." in admin.text
    assert "صفحه پیدا نشد." not in admin.text
