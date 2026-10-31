"""Username lockout stays above the per-IP limit, and recent sign-in IPs are exempt."""

import argparse
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.cli import cmd_reset_password
from app.config import Config
from app.db import _migrate_security_event, db
from app.main import create_app
from app.models import Admin, Setting, TelegramOutbox, TrustedLogin
from app.security import (
    clear_trusted_ips,
    hash_password,
    is_trusted_ip,
    login_limiter,
    remember_trusted_ip,
    verify_password,
)
from app.services.settings import settings
from app.timeutil import now_ts

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "fresh-password-1"
WRONG = "wrong-password"


def _post(app, ip, username, password):
    with TestClient(app, client=(ip, 5000)) as client:
        token = client.get("/admin/login").text
        token = token.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        response = client.post(
            "/admin/login",
            data={"csrf_token": token, "username": username, "password": password},
            follow_redirects=False,
        )
        page = ""
        if response.status_code == 303 and response.headers["location"].endswith("/login"):
            page = client.get("/admin/login").text
        return response, page


def _fill_user(username, count):
    key = f"user:{username.lower()}"
    for index in range(count):
        login_limiter.failure([f"ip:10.8.{index // 200}.{index % 200 + 1}", key])


def _assert_locked(response, page):
    assert response.status_code == 303 and response.headers["location"].endswith("/login")
    assert "Too many failed attempts" in page


@pytest.fixture
def app(tmp_path):
    settings._cache.clear()
    settings._loaded = False
    login_limiter._failures.clear()
    login_limiter._alert_at = None
    cfg = Config(db_path=tmp_path / "login.db", secret_key="test-secret", xray_bin=tmp_path / "no-xray",
                 log_dir=tmp_path / "logs", cookie_secure=False)
    application = create_app(cfg, background=False)
    Admin.create(username="admin", password_hash=hash_password(PASSWORD))
    yield application


def test_five_failures_lock_that_ip_only(app):
    for _ in range(5):
        response, page = _post(app, "10.1.0.1", "admin", WRONG)
        assert response.headers["location"].endswith("/login")
        assert "Incorrect username or password." in page
    locked, page = _post(app, "10.1.0.1", "admin", PASSWORD)
    _assert_locked(locked, page)
    other, _page = _post(app, "10.1.0.2", "admin", PASSWORD)
    assert other.status_code == 303 and other.headers["location"] == "/admin/"


def test_username_lock_spares_an_ip_that_signed_in(app):
    first, _page = _post(app, "10.2.0.2", "admin", PASSWORD)
    assert first.headers["location"] == "/admin/"
    _fill_user("admin", 49)
    crossing, page = _post(app, "10.3.0.49", "admin", WRONG)
    assert "Incorrect username or password." in page
    blocked, page = _post(app, "10.4.0.4", "admin", PASSWORD)
    _assert_locked(blocked, page)
    trusted, _page = _post(app, "10.2.0.2", "admin", PASSWORD)
    assert trusted.status_code == 303 and trusted.headers["location"] == "/admin/"


def test_password_form_clears_trusted_ips(app):
    with TestClient(app, client=("10.5.0.5", 5000)) as client:
        token = client.get("/admin/login").text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": PASSWORD},
                    follow_redirects=False)
        token = client.get("/admin/password").text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        saved = client.post("/admin/password", data={
            "csrf_token": token, "current": PASSWORD, "new": NEW_PASSWORD, "confirm": NEW_PASSWORD,
        })
        assert "Password changed." in saved.text
    assert TrustedLogin.select().count() == 0
    admin = Admin.get()
    assert verify_password(NEW_PASSWORD, admin.password_hash)
    _fill_user("admin", 50)
    blocked, page = _post(app, "10.5.0.5", "admin", NEW_PASSWORD)
    _assert_locked(blocked, page)


def test_cli_reset_password_clears_trusted_ips(app, monkeypatch):
    admin = Admin.get()
    remember_trusted_ip(admin.id, "10.6.0.6")
    assert TrustedLogin.select().count() == 1
    monkeypatch.setattr("app.config.get_config", lambda: Config(db_path=Path(str(db.database))))
    monkeypatch.setattr("sys.stdin", io.StringIO(NEW_PASSWORD + "\n"))
    cmd_reset_password(argparse.Namespace(username="admin", password_stdin=True))
    assert TrustedLogin.select().count() == 0
    admin = Admin.get()
    assert verify_password(NEW_PASSWORD, admin.password_hash)
    assert not verify_password(PASSWORD, admin.password_hash)
    _fill_user("admin", 50)
    blocked, page = _post(app, "10.6.0.6", "admin", NEW_PASSWORD)
    _assert_locked(blocked, page)
    clear_trusted_ips(admin.id)


def test_trusted_list_keeps_the_newest_twenty(database):
    admin = Admin.create(username="admin", password_hash="x")
    now = now_ts()
    for index in range(25):
        remember_trusted_ip(admin.id, f"10.0.0.{index + 1}", now=now + index)
    rows = list(TrustedLogin.select().order_by(TrustedLogin.last_success))
    assert len(rows) == 20
    assert rows[0].ip == "10.0.0.6"
    assert is_trusted_ip(admin.id, rows[0].ip, now=rows[0].last_success + 30 * 86400)
    assert not is_trusted_ip(admin.id, rows[0].ip, now=rows[0].last_success + 30 * 86400 + 1)


def test_security_event_is_added_to_a_saved_list_once(database):
    Setting.delete().where(Setting.key.in_(["telegram_events", "security_event_added"])).execute()
    Setting.create(key="telegram_events", value="down,up")
    _migrate_security_event()
    assert Setting.get(Setting.key == "telegram_events").value == "down,up,security"
    Setting.update(value="down,up").where(Setting.key == "telegram_events").execute()
    _migrate_security_event()
    assert Setting.get(Setting.key == "telegram_events").value == "down,up"


def test_security_event_migration_skips_an_unset_list(database):
    Setting.delete().where(Setting.key.in_(["telegram_events", "security_event_added"])).execute()
    _migrate_security_event()
    assert Setting.get_or_none(Setting.key == "telegram_events") is None
    assert Setting.get(Setting.key == "security_event_added").value == "1"


def test_security_alert_is_once_and_skips_unknown_names(app):
    settings.set("telegram_enabled", True)
    settings.set("telegram_token", "123456:ABCDEFGHIJKLMNOPQRSTUV")
    settings.set("telegram_chat_ids", "42")
    assert "security" in settings.telegram_events()
    name = "a<b>&c"
    Admin.create(username=name, password_hash=hash_password(PASSWORD))

    _fill_user("ghost-user", 49)
    _post(app, "10.4.0.1", "ghost-user", WRONG)
    assert TelegramOutbox.select().count() == 0

    _fill_user(name, 49)
    _post(app, "10.4.0.2", name, WRONG)
    rows = list(TelegramOutbox.select())
    assert len(rows) == 1
    assert '🔐 Many failed admin logins for "a&lt;b&gt;&amp;c"' in rows[0].text
    assert "a<b>" not in rows[0].text
    assert "Sign-ins from IPs that recently signed in still work." in rows[0].text

    login_limiter._failures.clear()
    _fill_user(name, 49)
    _post(app, "10.4.0.3", name, WRONG)
    assert TelegramOutbox.select().count() == 1
