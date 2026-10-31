"""Subscription URLs are fetched once. Stored subscriptions are gone."""

import asyncio
import base64
import time

import httpx
import pytest

from app.db import init_db
from app.models import Check, Incident, Server
from app.services import importer
from app.services.backup import FORMAT, import_data
from app.services.settings import settings
from tests.conftest import UUID, add_checks, make_server
from tests.test_web import app_factory, client, csrf_from, login  # noqa: F401

LINK = "vless://{uuid}@{host}:443#{name}"


def link(name: str, host: str) -> str:
    return LINK.format(uuid=UUID, host=host, name=name)


def encode(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture(autouse=True)
def _empty_fetch_cache():
    importer.clear_cache()
    yield
    importer.clear_cache()


def _transport(handler):
    return httpx.MockTransport(handler)


def _patch_fetch(monkeypatch, handler, resolve=None):
    lookup = resolve or (lambda host: ["1.1.1.1"])
    transport = _transport(handler)
    real = importer.fetch_text

    async def wrapped(url, proxy_pool, timeout=20.0, transport=None, resolver=None):  # noqa: ARG001
        return await real(url, proxy_pool, timeout=timeout, transport=transport or _transport(handler), resolver=lookup)

    monkeypatch.setattr(importer, "fetch_text", wrapped)
    monkeypatch.setattr(importer, "_resolve", lookup)


def test_migration_keeps_servers_and_history(tmp_path):
    path = tmp_path / "old.db"
    database = init_db(path)
    settings._cache.clear()
    settings._loaded = False
    database.execute_sql(
        "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, name TEXT, url TEXT, interval_minutes INTEGER, "
        "auto_sync INTEGER, make_public INTEGER, last_sync INTEGER, last_error TEXT, last_count INTEGER, "
        "created_at INTEGER)")
    database.execute_sql("INSERT INTO subscriptions (id, name, url) VALUES (1, 'Old', 'https://sub.example/secret')")
    database.execute_sql("ALTER TABLE servers ADD COLUMN subscription_id INTEGER")
    server = make_server("Kept", port=22001)
    database.execute_sql("UPDATE servers SET subscription_id = 1 WHERE id = ?", (server.id,))
    add_checks(server, 1_700_000_000, "oof")
    check_id = Check.select().get().id
    database.close()

    database = init_db(path)
    try:
        kept = Server.get()
        assert kept.name == "Kept"
        assert Check.get_by_id(check_id).server_id == kept.id
        assert "subscriptions" not in database.get_tables()
        assert "subscription_id" not in {column.name for column in database.get_columns("servers")}
        init_db(path)  # idempotent
        assert Server.get().name == "Kept"
    finally:
        database.close()


def test_old_backup_imports_servers_and_ignores_subscriptions(database):
    payload = {
        "format": FORMAT,
        "subscriptions": [{"id": 7, "name": "S", "url": "https://sub.example/secret-token"}],
        "servers": [{
            "name": "FromSub",
            "raw_link": link("FromSub", "from.example.com"),
            "subscription_id": 7,
            "is_public": False,
            "is_active": True,
            "tags": "edge",
        }],
        "settings": {},
    }
    summary = import_data(payload)
    assert summary["servers"] == 1
    assert "subscriptions" not in summary
    server = Server.get()
    assert server.name == "FromSub" and server.is_public is False and server.tags == "edge"
    assert "secret-token" not in str(summary)


def test_old_subscription_routes(client):
    login(client)
    token = csrf_from(client.get("/admin/servers").text)
    gone = client.get("/admin/subscriptions", follow_redirects=False)
    assert gone.status_code == 303 and gone.headers["location"].endswith("/servers")
    assert client.post("/admin/subscriptions/add", data={"csrf_token": token, "url": "https://sub.example/a"}).status_code == 404
    assert client.post("/admin/subscriptions/1/sync", data={"csrf_token": token}).status_code == 404
    assert client.post("/admin/subscriptions/1/delete", data={"csrf_token": token}).status_code == 404


def test_mixed_paste_adds_share_and_subscription(client, monkeypatch):
    body = "\n".join([link("Alpha", "alpha.example.com"), link("Beta", "beta.example.com"), "not-a-link"])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "sub.example.com":
            return httpx.Response(200, text=encode(body))
        return httpx.Response(403, text="no")

    _patch_fetch(monkeypatch, handler)
    login(client)
    token = csrf_from(client.get("/admin/servers").text)
    paste = "\n".join([
        link("Share", "share.example.com"),
        "https://sub.example.com/user-token",
        link("Alpha", "alpha.example.com"),
        "broken-line",
        "https://no.example.com/user-token",
    ])
    page = client.post("/admin/servers/add", data={"csrf_token": token, "links": paste, "is_public": "1"},
                       follow_redirects=True)
    assert page.status_code == 200
    names = sorted(server.name for server in Server.select())
    assert names == ["Alpha", "Beta", "Share"]
    assert "Added 3 servers (1 from 1 subscription link)." in page.text
    assert "1 duplicate skipped." in page.text
    assert "1 link failed: no.example.com (HTTP 403)." in page.text
    assert "user-token" not in page.text
    assert "Line 4:" in page.text
    assert any(url.startswith("https://sub.example.com/") for url in calls)


def test_preview_uses_the_cache_for_the_save(client, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=link("Cached", "cached.example.com"))

    _patch_fetch(monkeypatch, handler)
    login(client)
    token = csrf_from(client.get("/admin/servers").text)
    headers = {"X-CSRF-Token": token}
    paste = "https://sub.example.com/user-token"
    preview = client.post("/admin/servers/preview", json={"links": paste}, headers=headers).json()
    assert preview["subscriptions"][0]["new"] == 1
    assert preview["subscriptions"][0]["url"].startswith("https://sub.example.com/")
    saved = client.post("/admin/servers/add", data={"csrf_token": token, "links": paste, "is_public": "1"},
                        follow_redirects=True)
    assert "Added 1 server (1 from 1 subscription link)." in saved.text
    assert calls["n"] == 1
    assert Server.get().name == "Cached"


def test_local_subscription_urls_are_rejected(client, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=link("Nope", "nope.example.com"))

    def resolve(host):
        if host == "evil.example":
            return ["127.0.0.1"]
        return ["1.1.1.1"]

    _patch_fetch(monkeypatch, handler, resolve)
    login(client)
    token = csrf_from(client.get("/admin/servers").text)
    paste = "\n".join([
        "http://127.0.0.1/internal/proxy",
        "http://localhost/secret-path",
        "https://evil.example/secret-path",
    ])
    page = client.post("/admin/servers/add", data={"csrf_token": token, "links": paste}, follow_redirects=True)
    assert Server.select().count() == 0
    assert calls == []
    assert "127.0.0.1" in page.text or "localhost" in page.text
    assert "secret-path" not in page.text
    assert "internal/proxy" not in page.text
    assert page.text.count("local address") == 3


def test_more_than_ten_subscription_links_are_rejected(client, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=link("Nope", "nope.example.com"))

    _patch_fetch(monkeypatch, handler)
    login(client)
    token = csrf_from(client.get("/admin/servers").text)
    paste = "\n".join(f"https://sub{i}.example.com/a" for i in range(11))
    page = client.post("/admin/servers/add", data={"csrf_token": token, "links": paste, "is_public": "1"},
                       follow_redirects=True)
    assert "At most 10 subscription links" in page.text
    assert Server.select().count() == 0
    assert calls == []
    preview = client.post("/admin/servers/preview", json={"links": paste},
                          headers={"X-CSRF-Token": token}).json()
    assert "At most 10" in preview["error"]
    assert calls == []


def test_bulk_public_hidden_and_delete(client):
    login(client)
    first = make_server("One", port=23001, public=True)
    second = make_server("Two", port=23002, public=True)
    missing = make_server("Gone", port=23003)
    add_checks(first, 1_700_000_000, "oo")
    Incident.create(server=first, started_at=1_700_000_000, error_type="timeout")
    token = csrf_from(client.get("/admin/servers").text)
    headers = {"X-CSRF-Token": token}

    assert client.post("/admin/servers/bulk", json={"action": "hidden", "ids": [first.id, second.id, 9999]}).status_code == 400
    hidden = client.post("/admin/servers/bulk", json={"action": "hidden", "ids": [first.id, second.id, 9999]}, headers=headers)
    assert hidden.json() == {"ok": True, "changed": 2}
    assert Server.get_by_id(first.id).is_public is False
    assert Server.get_by_id(second.id).is_public is False
    shown = client.post("/admin/servers/bulk", json={"action": "public", "ids": [second.id]}, headers=headers)
    assert shown.json()["changed"] == 1
    assert Server.get_by_id(second.id).is_public is True

    deleted = client.post("/admin/servers/bulk", json={"action": "delete", "ids": [first.id, missing.id]}, headers=headers)
    assert deleted.json() == {"ok": True, "changed": 2}
    assert Server.select().count() == 1
    assert Server.get().name == "Two"
    assert Check.select().count() == 0
    assert Incident.select().count() == 0
    public = client.get("/")
    assert "Two" in public.text and "One" not in public.text and "Gone" not in public.text


def test_bulk_requires_login_and_csrf(client):
    server = make_server("Locked", port=23010)
    assert client.post("/admin/servers/bulk", json={"action": "delete", "ids": [server.id]}).status_code == 401
    login(client)
    assert Server.select().count() == 1


def test_slow_resolver_does_not_block_the_event_loop():
    def slow(host):
        time.sleep(2)
        return ["1.1.1.1"]

    async def run():
        async def nap():
            started = time.perf_counter()
            await asyncio.sleep(0.1)
            return time.perf_counter() - started

        pending = asyncio.create_task(importer.assert_not_local("https://sub.example.com/abc", resolver=slow))
        await asyncio.sleep(0)
        elapsed = await nap()
        await pending
        assert elapsed < 0.5

    asyncio.run(run())


def test_resolve_timeout_rejects_the_host(monkeypatch):
    monkeypatch.setattr(importer, "RESOLVE_TIMEOUT", 0.05)

    def slow(host):
        time.sleep(0.4)
        return ["1.1.1.1"]

    async def run():
        with pytest.raises(importer.ImportError) as caught:
            await importer.assert_not_local("https://sub.example.com/secret", resolver=slow)
        assert caught.value.host == "sub.example.com"
        assert caught.value.reason == "could not resolve host"

    asyncio.run(run())
