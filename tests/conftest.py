import json

import pytest

from app.db import init_db
from app.models import Check, Server
from app.services.settings import settings

UUID = "d342d11e-d424-4583-b36e-524ab1f0afa4"


@pytest.fixture
def database(tmp_path):
    database = init_db(tmp_path / "test.db")
    settings._cache.clear()
    settings._loaded = False
    yield database
    database.close()


def make_server(name="S", active=True, public=True, port=None, sort_order=0, address="hidden.example.com"):
    outbound = {"protocol": "vless", "settings": {"vnext": [{"address": address, "port": 443,
                                                            "users": [{"id": UUID, "encryption": "none"}]}]},
                "streamSettings": {"network": "tcp", "security": "none"}}
    return Server.create(
        name=name, raw_link=f"vless://{UUID}@{address}:443#{name}", protocol="vless", address=address, port=443,
        parsed_outbound=json.dumps(outbound), identity=name, is_active=active, is_public=public,
        socks_port=port, sort_order=sort_order,
    )


def add_checks(server, start, statuses, step=60, latency=100):
    """statuses: string of 'o' (ok), 'f' (fail), 'u' (unknown)."""
    rows = []
    for i, code in enumerate(statuses):
        status = {"o": "ok", "f": "fail", "u": "unknown"}[code]
        rows.append({"server": server.id, "checked_at": start + i * step, "status": status,
                     "latency_ms": (latency(i) if callable(latency) else latency) if status == "ok" else None,
                     "error_type": None if status == "ok" else "timeout"})
    Check.insert_many(rows).execute()
