"""Builds and wires all services; owns the background tasks."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable

from app.config import BASE_DIR, Config
from app.db import run_db
from app.models import Server
from app.services import events as ev
from app.services.aggregator import Aggregator
from app.services.checker import Checker
from app.services.control import Control
from app.services.incidents import IncidentService
from app.services.monitor import Monitor
from app.services.proxy_pool import ProxyPool
from app.services.stats import StatsService
from app.services.status import RECENT_WINDOW, LiveRegistry, seed_rows
from app.services.subscriptions import SubscriptionService
from app.services.telegram.bot_commands import BotPoller
from app.services.telegram.notifier import TelegramNotifier
from app.services.telegram.outbox_worker import OutboxWorker
from app.services.telegram.sender import TelegramSender
from app.services.xray_manager import XrayManager
from app.timeutil import now_ts

log = logging.getLogger(__name__)


class Services:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.live = LiveRegistry()
        self.incidents = IncidentService()
        self.stats = StatsService(self.live, self.incidents.in_incident)
        self.xray = XrayManager(cfg, on_event=self._xray_event)
        self.proxy_pool = ProxyPool(self.xray, self.stats)
        self.sender = TelegramSender(self.proxy_pool)
        self.outbox = OutboxWorker(self.sender)
        self.notifier = TelegramNotifier(self.outbox, self.sender, self.stats)
        self.bot = BotPoller(self.sender, self.outbox, self.notifier.respond)
        self.monitor = Monitor(self.live, self.incidents, self.stats, self.notifier.handle_event)
        self.checker = Checker(self.xray, self.monitor.handle_cycle)
        self.aggregator = Aggregator(on_done=self.stats.invalidate)
        self.subscriptions = SubscriptionService(self.proxy_pool, self.after_servers_changed)
        self.control = Control(BASE_DIR)
        self.tasks: list[asyncio.Task[None]] = []
        self.started_at: int | None = None

    def _xray_event(self, kind: str, data: dict) -> None:
        self.notifier.handle_event(ev.Event(kind, now_ts(), data=data))

    async def start(self, background: bool = True) -> None:
        self.started_at = now_ts()
        await run_db(self.incidents.restore)
        self.live.seed(await run_db(seed_rows, now_ts() - RECENT_WINDOW))
        if not background:
            return
        await self.xray.start()
        jobs: dict[str, Callable[[], Awaitable[None]]] = {
            "checker": self.checker.run_forever,
            "aggregator": self.aggregator.run_forever,
            "outbox": self.outbox.run_forever,
            "bot": self.bot.run_forever,
            "reports": self.notifier.reports_forever,
            "subscriptions": self.subscriptions.run_forever,
            "xray-supervisor": self.xray.supervise_forever,
        }
        self.tasks = [asyncio.create_task(self._supervised(name, job), name=name) for name, job in jobs.items()]
        active = await run_db(lambda: Server.select().where(Server.is_active == True).count())  # noqa: E712
        self.notifier.handle_event(ev.Event(ev.PANEL_STARTED, self.started_at, data={"servers": active}))

    @staticmethod
    async def _supervised(name: str, job: Callable[[], Awaitable[None]]) -> None:
        """Keep a background loop alive: log and restart it if it ever crashes."""
        while True:
            try:
                await job()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background task crashed; restarting", extra={"task": name})
                await asyncio.sleep(5)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.xray.stop()

    async def after_servers_changed(self, deactivated: Iterable[int] = ()) -> None:
        """Call after any server change: closes incidents of removed servers, hot-reloads Xray."""
        for server_id in deactivated:
            await run_db(self.incidents.close_for_server, server_id)
            self.live.remove(server_id)
        await self.xray.reconcile()
        self.stats.invalidate()
