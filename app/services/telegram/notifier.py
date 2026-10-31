"""Turns monitoring events into queued Telegram messages; schedules reports."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import __version__
from app.db import run_db
from app.models import Incident, Server
from app.services import events as ev
from app.services.settings import settings
from app.services.stats import StatsService, incident_stats, rank_fastest, rank_stable, window_counts
from app.services.telegram import messages
from app.services.telegram.outbox_worker import OutboxWorker
from app.services.telegram.sender import TelegramSender
from app.timeutil import DAY, now_ts, to_local

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, outbox: OutboxWorker, sender: TelegramSender, stats: StatsService) -> None:
        self.outbox = outbox
        self.sender = sender
        self.stats = stats
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def wants(category: str) -> bool:
        return bool(settings.get("telegram_enabled") and settings.get("telegram_token")
                    and settings.chat_ids() and category in settings.telegram_events())

    # ---------------------------------------------------------- events
    def handle_event(self, event: ev.Event) -> None:
        """Called from the event loop; never blocks the caller."""
        if not self.wants(ev.CATEGORY.get(event.kind, "")):
            return
        task = asyncio.get_running_loop().create_task(self._handle(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, event: ev.Event) -> None:
        text = await run_db(self.render_event, event)
        if text:
            exclude = event.server_id if event.kind in (ev.DOWN, ev.FLAPPING) else None
            await self.outbox.enqueue(event.kind, text, event.at, exclude_server_id=exclude)

    def render_event(self, event: ev.Event) -> str | None:
        """Sync (DB): build the message text for an event."""
        d = event.data
        name = ""
        if event.server_id is not None:
            server = Server.get_or_none(Server.id == event.server_id)
            if server is None:
                return None
            name = server.name
        if event.kind == ev.DOWN:
            uptime = window_counts(event.at - DAY, event.at, event.server_id).get(event.server_id)
            return messages.server_down(name, d.get("started_at", event.at), d.get("error_type"),
                                        uptime.uptime if uptime else None)
        if event.kind == ev.UP:
            return messages.server_up(name, d["started_at"], d["ended_at"], d.get("duration"))
        if event.kind == ev.FLAPPING:
            return messages.flapping(name, d.get("count", 0), d.get("window_minutes", 30), event.at)
        if event.kind == ev.FLAP_END:
            return messages.flap_end(name, d.get("in_incident", False), event.at)
        if event.kind == ev.NETWORK_DOWN:
            return messages.network_down(event.at, d.get("servers", 0))
        if event.kind == ev.NETWORK_UP:
            return messages.network_up(d["started_at"], event.at)
        if event.kind == ev.XRAY_CRASH:
            return messages.xray_crash(event.at, d.get("code"), d.get("detail", ""))
        if event.kind == ev.XRAY_RESTARTED:
            return messages.xray_restarted(event.at, d.get("version"))
        if event.kind == ev.PANEL_STARTED:
            return messages.panel_started(event.at, __version__, d.get("servers", 0))
        return None

    # ---------------------------------------------------------- reports
    def build_report(self, days: int, now: int | None = None) -> list[str]:
        now = now or now_ts()
        since = now - days * DAY
        servers = list(Server.select().where(Server.is_active == True).order_by(Server.sort_order, Server.id))  # noqa: E712
        counts = window_counts(since, now)
        incidents = incident_stats(since, now)
        rows = []
        for server in servers:
            c = counts.get(server.id)
            st = incidents.get(server.id)
            rows.append(messages.ReportRow(server.name, c.uptime if c else None, st.count if st else 0,
                                           st.downtime if st else 0, c.avg_latency if c else None))
        metrics = self.stats.server_metrics()
        names = {s.id: s.name for s in servers}
        fastest_ids = rank_fastest(metrics.values())
        stable_ids = rank_stable(metrics.values())
        fastest = (names[fastest_ids[0]], metrics[fastest_ids[0]].median_1h) if fastest_ids else None
        stable = (names[stable_ids[0]], metrics[stable_ids[0]].stability) if stable_ids else None
        title = "Daily report" if days == 1 else "Weekly report"
        period = "24h" if days == 1 else "7d"
        return messages.report(title, period, rows, fastest, stable, now)

    def build_status(self) -> list[str]:
        now = now_ts()
        metrics = self.stats.server_metrics()
        servers = Server.select().where(Server.is_active == True).order_by(Server.sort_order, Server.id)  # noqa: E712
        rows = []
        for server in servers:
            m = metrics.get(server.id)
            rows.append((server.name, self.stats.current_status(server.id, now),
                         m.latency_ms if m else None, m.uptime_24h if m else None))
        return messages.status_overview(rows, now)

    @staticmethod
    def build_incidents() -> list[str]:
        query = (Incident.select(Incident, Server).join(Server)
                 .order_by(Incident.started_at.desc()).limit(10))
        rows = [(i.server.name, i.started_at, i.ended_at, i.error_type) for i in query]
        return messages.incidents_overview(rows, now_ts())

    async def respond(self, command: str) -> list[str]:
        if command == "/status":
            return await run_db(self.build_status)
        if command == "/report":
            return await run_db(self.build_report, 1)
        if command == "/incidents":
            return await run_db(self.build_incidents)
        if command in ("/start", "/help"):
            return [messages.help_text()]
        return []

    async def reports_forever(self) -> None:
        while True:
            try:
                await self.maybe_send_reports()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("report scheduling failed")
            await asyncio.sleep(30)

    async def maybe_send_reports(self, now: int | None = None) -> list[str]:
        now = now or now_ts()
        local = to_local(now, settings.tz)
        today = local.date().isoformat()
        hour, minute = (int(x) for x in settings.get("telegram_daily_time").split(":"))
        due_time = (local.hour, local.minute) >= (hour, minute)
        sent: list[str] = []
        for key, kind, days in (("last_daily_report", "daily", 1), ("last_weekly_report", "weekly", 7)):
            last = settings.get(key)
            if not last:  # first run: start counting from today, don't send immediately
                await run_db(settings.set, key, today, True)
                continue
            if kind == "weekly" and local.weekday() != int(settings.get("telegram_weekly_day")):
                continue
            if not due_time or last == today:
                continue
            await run_db(settings.set, key, today, True)
            if not self.wants(kind):
                continue
            for text in await run_db(self.build_report, days, now):
                await self.outbox.enqueue(kind, text, now)
            sent.append(kind)
        return sent

    # ---------------------------------------------------------- test
    async def send_test(self) -> list[dict[str, Any]]:
        """Send immediately (not queued) to every chat; report the route used."""
        results = []
        now = now_ts()
        for chat_id in settings.chat_ids():
            result = await self.sender.call(
                "sendMessage", {}, payload_for_route=lambda route, chat=chat_id: {
                    "chat_id": chat, "text": messages.test_message(now, route.label), "parse_mode": "HTML"})
            results.append({"chat_id": chat_id, "ok": result.ok, "error": result.error, "tried": result.tried,
                            "via": result.route.label if result.ok and result.route else None})
        return results
