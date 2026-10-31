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
    fragment = client.get("/partials/live?range=30d")
    assert fragment.status_code == 200 and 'data-range="30d"' in fragment.text


def test_history_404_for_hidden_and_inactive(client, seeded):
    visible, hidden, inactive = seeded
    assert client.get(f"/api/servers/{visible.id}/history?range=24h").status_code == 200
    assert client.get(f"/api/servers/{hidden.id}/history").status_code == 404
    assert client.get(f"/api/servers/{inactive.id}/history").status_code == 404
    assert client.get("/api/servers/9999/history").status_code == 404


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
    assert dashboard.status_code == 200 and "داشبورد" in dashboard.text
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
    assert "تلاش‌های ناموفق" in client.get("/admin/login").text


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
    assert "خط ۳" in response.text or "خط 3" in response.text
    bad = client.post("/admin/settings", data={"csrf_token": token, "check_interval": "5"}, follow_redirects=True)
    assert "نباید کمتر" in bad.text
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
