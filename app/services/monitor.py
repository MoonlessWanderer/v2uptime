"""Glue between the checker and everything that consumes its results.

For each completed cycle: store all checks in one transaction, update the
live registry, run the N/M incident logic, detect monitor-network outages,
invalidate cached stats and hand resulting events to the notifier.
"""

from __future__ import annotations

import logging
from typing import Callable

from app.db import db, run_db
from app.models import Check, Server
from app.services import events as ev
from app.services.checker import CheckRecord, CycleReport
from app.services.incidents import IncidentService
from app.services.stats import StatsService
from app.services.status import LiveRegistry, LivePoint

log = logging.getLogger(__name__)


def save_checks(records: list[CheckRecord]) -> None:
    rows = [{
        "server": r.server_id, "checked_at": r.checked_at, "status": r.status, "latency_ms": r.latency_ms,
        "error_type": r.error_type, "error_detail": (r.error_detail or "")[:300] or None,
        "connect_ms": r.connect_ms,
    } for r in records]
    with db.atomic():
        for start in range(0, len(rows), 300):
            Check.insert_many(rows[start:start + 300]).execute()


class Monitor:
    def __init__(self, live: LiveRegistry, incidents: IncidentService, stats: StatsService,
                 notify: Callable[[ev.Event], None]) -> None:
        self.live = live
        self.incidents = incidents
        self.stats = stats
        self.notify = notify
        self.network_down_since: int | None = None

    def network_events(self, report: CycleReport) -> list[ev.Event]:
        if report.network_down and self.network_down_since is None:
            self.network_down_since = report.started_at
            return [ev.Event(ev.NETWORK_DOWN, report.started_at, data={"servers": report.checked})]
        if not report.network_down and self.network_down_since is not None and report.records:
            since, self.network_down_since = self.network_down_since, None
            return [ev.Event(ev.NETWORK_UP, report.started_at,
                             data={"started_at": since, "duration": report.started_at - since})]
        return []

    async def handle_cycle(self, report: CycleReport) -> None:
        # Records of servers deleted while the cycle ran would violate the FK.
        existing = await run_db(lambda: {sid for (sid,) in Server.select(Server.id).tuples()})
        records = [r for r in report.records if r.server_id in existing]
        await run_db(save_checks, records)
        for r in records:
            self.live.add(r.server_id, LivePoint(r.checked_at, r.status, r.latency_ms, r.error_type))
        events = await run_db(self.incidents.process, records)
        events += self.network_events(report)
        self.stats.invalidate()
        for event in events:
            try:
                self.notify(event)
            except Exception:
                log.exception("notify failed", extra={"kind": event.kind})
