"""Monitor network outage alerts across a panel restart."""

import asyncio
from unittest.mock import MagicMock

from app.services import events as ev
from app.services.checker import CycleReport
from app.services.monitor import Monitor
from app.services.settings import settings

START = 1_700_000_000
END = START + 8 * 3600


def monitor():
    return Monitor(MagicMock(), MagicMock(), MagicMock(), lambda event: None)


def restarted():
    fresh = monitor()
    fresh.restore()
    return fresh


def report(at, down):
    return CycleReport(started_at=at, network_down=down, checked=2, records=[object()])


def test_restore_alert_survives_a_restart_during_the_outage(database):
    before = monitor()
    assert [e.kind for e in before.network_events(report(START, True))] == [ev.NETWORK_DOWN]

    after = restarted()
    assert after.network_down_since == START
    [event] = after.network_events(report(END, False))
    assert event.kind == ev.NETWORK_UP
    assert event.data == {"started_at": START, "duration": END - START}


def test_a_restart_during_the_outage_does_not_repeat_the_down_alert(database):
    monitor().network_events(report(START, True))
    assert restarted().network_events(report(START + 60, True)) == []


def test_finished_outage_is_forgotten(database):
    first = monitor()
    first.network_events(report(START, True))
    first.network_events(report(END, False))
    assert settings.get("network_down_since") == 0

    after = restarted()
    assert after.network_down_since is None
    assert after.network_events(report(END + 60, False)) == []


def test_a_cycle_stores_the_outage_start(database):
    live, incidents, stats = MagicMock(), MagicMock(), MagicMock()
    incidents.process.return_value = []
    sent = []
    panel = Monitor(live, incidents, stats, sent.append)
    asyncio.run(panel.handle_cycle(CycleReport(started_at=START, network_down=True, checked=2)))
    assert settings.get("network_down_since") == START
    assert [e.kind for e in sent] == [ev.NETWORK_DOWN]
