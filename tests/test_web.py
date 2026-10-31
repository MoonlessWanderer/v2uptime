import re

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.models import Admin, Server
from app.security import hash_password, login_limiter
from app.services.settings import settings
from app.timeutil import now_ts

from tests.conftest import add_checks, make_server

PASSWORD = "correct-horse-battery"


@pytest.fixture
def app_factory(tmp_path):
    def build(cookie_secure=False):
        settings._cache.clear()
        settings._loaded = False
        cfg = Config(db_path=tmp_path / "web.db", secret_key="test-secret", xray_bin=tmp_path / "no-xray",
                     log_dir=tmp_path / "logs", cookie_secure=cookie_secure)
        return create_app(cfg, background=False)
    login_limiter._failures.clear()
    return build


@pytest.fixture
def client(app_factory):
    app = app_factory()
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        yield c


@pytest.fixture
def seeded(client):
    now = now_ts()
    visible = make_server("Visible <A&B>", address="secret-host.example.com", port=20001)
    hidden = make_server("HiddenOne", public=False, port=20002)
    inactive = make_server("InactiveOne", active=False, port=20003)
    add_checks(visible, now - 1200, "o" * 20)
    return visible, hidden, inactive


def csrf_from(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, password=PASSWORD):
    Admin.get_or_create(username="admin", defaults={"password_hash": hash_password(PASSWORD)})
    token = csrf_from(client.get("/admin/login").text)
    return client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": password},
                       follow_redirects=False)


# ----------------------------------------------------------------- public

def test_public_page_and_api_hide_sensitive_data(client, seeded):
    visible, hidden, inactive = seeded
    page = client.get("/")
    assert page.status_code == 200
    assert "Visible &lt;A&amp;B&gt;" in page.text
    for secret in ("secret-host", "HiddenOne", "InactiveOne", "vless://", "20001"):
        assert secret not in page.text
    data = client.get("/api/status?range=7d").json()
    assert [s["name"] for s in data["servers"]] == ["Visible <A&B>"]
    assert len(data["servers"][0]["bars"]) == 42 and len(data["buckets"]) == 42
    assert "secret-host" not in str(data)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["generated_at"])
    assert data["range_label"] == "7d"
    assert data["timezone"]["name"] == "Asia/Tehran" and data["timezone"]["offset"] == "+03:30"
    assert data["buckets"][0]["start"].endswith("Z")
    assert data["servers"][0]["last_incident"] is None
    fragment = client.get("/partials/live?range=30d")
    assert fragment.status_code == 200 and 'data-range="30d"' in fragment.text


def test_history_404_for_hidden_and_inactive(client, seeded):
    visible, hidden, inactive = seeded
    history = client.get(f"/api/servers/{visible.id}/history?range=24h")
    assert history.status_code == 200
    body = history.json()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["points"][0]["t"])
    assert body["range_label"] == "24h" and body["timezone"]["offset"] == "+03:30"
    assert client.get(f"/api/servers/{hidden.id}/history").status_code == 404
    assert client.get(f"/api/servers/{inactive.id}/history").status_code == 404
    missing = client.get("/api/servers/9999/history")
    assert missing.status_code == 404 and missing.json()["detail"] == "Server not found"


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_internal_proxy_only_for_direct_local_requests(client):
    assert client.get("/internal/proxy", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404
    response = client.get("/internal/proxy")
    assert response.status_code == 503  # local, but no healthy server (no Xray in tests)
    assert client.get("/internal/proxy?format=text").text == ""


def test_internal_proxy_rejects_remote_clients(app_factory):
    with TestClient(app_factory(), client=("203.0.113.9", 4000)) as remote:
        assert remote.get("/internal/proxy").status_code == 404


# ------------------------------------------------------------------ admin

def test_admin_requires_login(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert client.post("/admin/servers/1/toggle", json={"flag": "is_active"}).status_code == 401


def test_login_requires_csrf(client):
    Admin.create(username="admin", password_hash=hash_password(PASSWORD))
    client.get("/admin/login")
    response = client.post("/admin/login", data={"username": "admin", "password": PASSWORD})
    assert response.status_code == 400


def test_login_success_and_dashboard(client, seeded):
    response = login(client)
    assert response.status_code == 303 and response.headers["location"] == "/admin/"
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200 and "Dashboard" in dashboard.text
    for path in ("/admin/servers", "/admin/subscriptions", "/admin/incidents", "/admin/settings",
                 "/admin/telegram", "/admin/password", "/admin/backup", f"/admin/servers/{seeded[0].id}",
                 f"/admin/servers/{seeded[0].id}/edit"):
        assert client.get(path).status_code == 200, path
    history = client.get(f"/admin/servers/{seeded[1].id}/history.json?range=7d")
    assert history.status_code == 200  # hidden servers are visible to the admin


def test_login_rate_limit(client):
    for _ in range(5):
        login(client, password="wrong-password")
    response = login(client)  # correct password, but blocked now
    assert response.headers["location"] == "/admin/login"
    assert "Too many failed attempts" in client.get("/admin/login").text


def test_session_cookie_flags(app_factory):
    with TestClient(app_factory(cookie_secure=True), base_url="https://testserver") as secure:
        response = login(secure)
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie


def test_admin_path_can_change(client):
    login(client)
    settings.set("admin_path", "/panel-x7k2")
    assert client.get("/admin/login").status_code == 404
    assert client.get("/__admin/login").status_code == 404
    assert client.get("/panel-x7k2/login", follow_redirects=False).status_code in (200, 303)
    assert client.get("/panel-x7k2").status_code == 200


def test_add_servers_and_settings_validation(client):
    login(client)
    page = client.get("/admin/servers")
    token = csrf_from(page.text)
    links = ("vless://d342d11e-d424-4583-b36e-524ab1f0afa4@a.example.com:443#One\n"
             "trojan://pw@b.example.com:443#Two\nbroken-line")
    response = client.post("/admin/servers/add", data={"csrf_token": token, "links": links, "is_public": "1"},
                           follow_redirects=True)
    assert response.status_code == 200
    assert Server.select().count() == 2
    assert "Line 3:" in response.text
    bad = client.post("/admin/settings", data={"csrf_token": token, "check_interval": "5"}, follow_redirects=True)
    assert "Must be at least" in bad.text
    assert settings.get("check_interval") == 60


def test_toggle_and_reorder_json(client):
    login(client)
    a, b = make_server("A", sort_order=1), make_server("B", sort_order=2)
    token = csrf_from(client.get("/admin/servers").text)
    headers = {"X-CSRF-Token": token}
    assert client.post(f"/admin/servers/{a.id}/toggle", json={"flag": "is_public"}).status_code == 400
    response = client.post(f"/admin/servers/{a.id}/toggle", json={"flag": "is_public"}, headers=headers)
    assert response.json() == {"ok": True, "flag": "is_public", "value": False}
    client.post("/admin/servers/reorder", json={"ids": [b.id, a.id]}, headers=headers)
    assert [s.name for s in Server.select().order_by(Server.sort_order)] == ["B", "A"]


def test_backup_roundtrip(client):
    login(client)
    make_server("Backed")
    exported = client.get("/admin/backup/export")
    assert exported.status_code == 200 and "Backed" in exported.text
    token = csrf_from(client.get("/admin/backup").text)
    Server.delete().execute()
    client.post("/admin/backup/import", data={"csrf_token": token, "mode": "merge"},
                files={"file": ("b.json", exported.content, "application/json")})
    assert [s.name for s in Server.select()] == ["Backed"]


def test_password_change_invalidates_other_sessions(app_factory):
    app = app_factory()
    with TestClient(app) as first, TestClient(app) as second:
        login(first)
        login(second)
        token = csrf_from(first.get("/admin/password").text)
        response = first.post("/admin/password", data={"csrf_token": token, "current": PASSWORD,
                                                       "new": "another-long-pass", "confirm": "another-long-pass"})
        assert response.status_code == 200
        assert first.get("/admin", follow_redirects=False).status_code == 200
        assert second.get("/admin", follow_redirects=False).status_code == 303


ARABIC = re.compile(r"[\u0600-\u06FF]")


def _assert_latin(label, text):
    match = ARABIC.search(text)
    assert match is None, f"{label} contains U+{ord(match.group()):04X}"


def test_rendered_pages_and_api_are_english(client, seeded):
    visible = seeded[0]
    _assert_latin("public", client.get("/").text)
    _assert_latin("login", client.get("/admin/login").text)
    login(client)
    for path in ("/admin", "/admin/servers", "/admin/subscriptions", "/admin/incidents", "/admin/settings",
                 "/admin/telegram", "/admin/password", "/admin/backup",
                 f"/admin/servers/{visible.id}", f"/admin/servers/{visible.id}/edit"):
        response = client.get(path)
        assert response.status_code == 200, path
        _assert_latin(path, response.text)
    status = client.get("/api/status?range=24h")
    history = client.get(f"/api/servers/{visible.id}/history")
    missing = client.get("/api/servers/9999/history")
    assert missing.json()["detail"] == "Server not found"
    for label, response in ("status", status), ("history", history), ("missing", missing):
        _assert_latin(label, response.text)


def test_persian_server_name_is_isolated(client):
    name = "آلمان"
    make_server(name, port=21001)
    page = client.get("/")
    assert page.status_code == 200
    assert f"<bdi>{name}</bdi>" in page.text
    login(client)
    admin = client.get("/admin/servers")
    assert f"<bdi>{name}</bdi>" in admin.text


def _row(html, server_id):
    match = re.search(rf'<li data-id="{server_id}".*?</li>', html, re.S)
    assert match, f"row {server_id} not found"
    return match.group(0)


def test_new_label_is_the_same_on_page_and_api(client):
    now = now_ts()
    fresh = make_server("Fresh", port=20011)
    add_checks(fresh, now - 120, "o")  # one check: still collecting data
    settled = make_server("Settled", port=20012)
    add_checks(settled, now - 3 * 3600, "o" * 20)  # enough data, but nothing recent: unknown, not collecting
    cards = {s["id"]: s for s in client.get("/api/status").json()["servers"]}
    assert (cards[fresh.id]["status"], cards[fresh.id]["status_label"], cards[fresh.id]["collecting"]) == \
        ("unknown", "New", True)
    assert (cards[settled.id]["status"], cards[settled.id]["status_label"], cards[settled.id]["collecting"]) == \
        ("unknown", "Unknown", False)
    for server, label in ((fresh, "New"), (settled, "Unknown")):
        history = client.get(f"/api/servers/{server.id}/history").json()["server"]
        assert history["status"] == "unknown" and history["status_label"] == label
        row_status = re.search(r'class="row-status[^"]*">(.*?)</span>\s*<span class="row-ping"',
                               _row(client.get("/").text, server.id), re.S).group(1)
        assert re.sub(r"<[^>]+>", "", row_status).strip() == label


# Remark -> stored country. Names are shown exactly as written; no flag is ever added.
REMARKS = [
    ("🇩🇪 Germany | Reality", "DE"),
    ("Germany 🇩🇪", "DE"),
    ("🇩🇪", "DE"),
    ("🇮🇷➡️🇩🇪 Tunnel", "DE"),
    ("🇩 broken 🇪", ""),
    ("Plain name", "NL"),  # no flag in the name; country set by hand
    ("آلمان 🇩🇪 ویژه", "DE"),
]


def _flag_letters(text):
    return [c for c in text if 0x1F1E6 <= ord(c) <= 0x1F1FF]


def _without_names(text):
    for name, _ in sorted(REMARKS, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(name, "").replace(name.lower(), "")
    return text


def test_names_are_shown_exactly_as_written(client):
    from urllib.parse import quote

    from app.services.telegram import messages
    from tests.conftest import UUID

    login(client)
    headers = {"X-CSRF-Token": csrf_from(client.get("/admin/servers").text)}
    links = "\n".join(f"vless://{UUID}@h{i}.example.com:443?type=tcp#{quote(name)}"
                      for i, (name, _) in enumerate(REMARKS))
    client.post("/admin/servers/add", data={"csrf_token": headers["X-CSRF-Token"], "links": links, "is_public": "1"})
    servers = {s.name: s for s in Server.select()}
    assert sorted(servers) == sorted(name for name, _ in REMARKS)
    plain = servers["Plain name"]
    assert client.post(f"/admin/servers/{plain.id}/edit", data={
        "csrf_token": headers["X-CSRF-Token"], "name": "Plain name", "country": "nl", "tags": "",
        "is_active": "1", "is_public": "1", "raw_link": plain.raw_link}, follow_redirects=False).status_code == 303
    servers = {s.name: s for s in Server.select()}
    assert {name: s.country for name, s in servers.items()} == dict(REMARKS)

    pages = {"public": client.get("/").text, "partial": client.get("/partials/live").text,
             "admin servers": client.get("/admin/servers").text, "dashboard": client.get("/admin/").text,
             "incidents": client.get("/admin/incidents").text}
    for name, server in servers.items():
        pages[f"detail {name}"] = client.get(f"/admin/servers/{server.id}").text
        pages[f"edit {name}"] = client.get(f"/admin/servers/{server.id}/edit").text
    for label, html in pages.items():
        assert not _flag_letters(_without_names(html)), f"extra flag on {label}"
    for name in servers:
        assert f"<bdi>{name}</bdi>" in pages["public"] and f"<bdi>{name}</bdi>" in pages["admin servers"]
    assert not _flag_letters(_row(pages["public"], servers["Plain name"].id))

    status = client.get("/api/status").json()
    assert sorted(s["name"] for s in status["servers"]) == sorted(servers)
    for card in status["servers"]:
        assert "flag" not in card and card["country"] == servers[card["name"]].country
        history = client.get(f"/api/servers/{card['id']}/history").json()["server"]
        assert history["name"] == card["name"] and "flag" not in history

    for name in servers:
        for text in (messages.server_down(name, now_ts(), "timeout", 99.0),
                     messages.server_up(name, now_ts() - 60, now_ts(), 60)):
            assert f"{messages.FSI}{name}{messages.PDI}" in text
            assert not _flag_letters(text.replace(name, ""))
