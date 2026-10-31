"""XrayManager against a fake xray process: start, hot reload, rejection, crash recovery."""

import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

from app.config import Config
from app.models import Server
from app.services.xray_manager import XrayManager, port_is_free

from tests.conftest import make_server

FAKE = Path(__file__).with_name("fake_xray.py")


@pytest.fixture
def manager_factory(database, tmp_path):
    managers = []

    def build():
        base = random.randint(30000, 50000)
        cfg = Config(db_path=tmp_path / "test.db", xray_bin=FAKE, xray_api_port=base - 1, socks_port_base=base)
        events = []
        manager = XrayManager(cfg, on_event=lambda kind, data: events.append(kind))
        manager.argv = lambda *args: [sys.executable, str(FAKE), *args]
        managers.append(manager)
        return manager, events

    yield build


def run(coro):
    return asyncio.run(coro)


def listening(port):
    return not port_is_free(port)


def test_start_hot_reload_and_rejection(manager_factory):
    manager, _ = manager_factory()
    a, b = make_server("A"), make_server("B")

    async def scenario():
        assert await manager.start()
        assert manager.version == "25.1.1"
        ports = manager.loaded_ports()
        assert set(ports) == {a.id, b.id} and all(listening(p) for p in ports.values())
        epoch = manager.epoch

        # add: hot, no restart
        c = await asyncio.to_thread(make_server, "C")
        await manager.reconcile()
        assert c.id in manager.loaded_ports() and manager.epoch == epoch
        assert listening(manager.loaded_ports()[c.id])

        # edit outbound: re-added on the same stable port
        port_b = manager.loaded_ports()[b.id]
        outbound = json.loads(b.parsed_outbound)
        outbound["streamSettings"]["network"] = "ws"
        await asyncio.to_thread(lambda: Server.update(parsed_outbound=json.dumps(outbound))
                                .where(Server.id == b.id).execute())
        await manager.reconcile()
        assert manager.loaded_ports()[b.id] == port_b and manager.epoch == epoch
        assert manager._loaded[b.id].outbound["streamSettings"]["network"] == "ws"

        # deactivate: inbound closed, others untouched
        port_a = manager.loaded_ports()[a.id]
        await asyncio.to_thread(lambda: Server.update(is_active=False).where(Server.id == a.id).execute())
        await manager.reconcile()
        assert a.id not in manager.loaded_ports() and not listening(port_a)
        assert manager.epoch == epoch

        # invalid outbound: rejected and marked, Xray keeps running
        bad = await asyncio.to_thread(make_server, "Bad")
        await asyncio.to_thread(lambda: Server.update(parsed_outbound=json.dumps({"protocol": "invalid"}))
                                .where(Server.id == bad.id).execute())
        await manager.reconcile()
        assert bad.id not in manager.loaded_ports() and manager.is_ready() and manager.epoch == epoch
        assert "unknown protocol" in Server.get_by_id(bad.id).xray_error
        await manager.stop()
        assert not manager.is_ready()

    run(scenario())


def test_invalid_server_does_not_block_startup(manager_factory):
    manager, _ = manager_factory()
    good = make_server("Good")
    bad = make_server("Bad")
    Server.update(parsed_outbound=json.dumps({"protocol": "invalid"})).where(Server.id == bad.id).execute()

    async def scenario():
        assert await manager.start()
        assert set(manager.loaded_ports()) == {good.id}
        await manager.stop()

    run(scenario())
    assert Server.get_by_id(bad.id).xray_error


def test_crash_is_detected_and_recovered(manager_factory):
    manager, events = manager_factory()
    make_server("A")

    async def scenario():
        assert await manager.start()
        epoch = manager.epoch
        manager._proc.kill()
        for _ in range(100):
            await asyncio.sleep(0.1)
            if manager.is_ready() and manager.epoch > epoch:
                break
        assert manager.is_ready() and manager.epoch >= epoch + 2  # crash + restart
        assert manager.crashes == 1 and manager.restarts == 1
        assert events == ["xray_crash", "xray_restarted"]
        await manager.stop()

    run(scenario())


def test_missing_binary(database, tmp_path):
    manager = XrayManager(Config(db_path=tmp_path / "x.db", xray_bin=tmp_path / "nope"))
    assert run(manager.start()) is False
    assert manager.status()["state"] == "missing"
