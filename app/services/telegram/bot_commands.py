"""Long-polling bot commands (/status, /report, /incidents).

Updates are fetched with ``getUpdates`` through the same route logic as
sending (direct or via an online server).  Only registered chat IDs get an
answer; everything else is ignored silently.  Replies go through the outbox.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.db import run_db
from app.services.settings import settings
from app.services.telegram.outbox_worker import OutboxWorker
from app.services.telegram.sender import TelegramSender
from app.timeutil import now_ts

log = logging.getLogger(__name__)

Responder = Callable[[str], Awaitable[list[str]]]
LONG_POLL = 25


def parse_command(text: str) -> str | None:
    """'/Status@MyBot arg' -> '/status'."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    return text.split()[0].split("@")[0].lower()


class BotPoller:
    def __init__(self, sender: TelegramSender, outbox: OutboxWorker, responder: Responder) -> None:
        self.sender = sender
        self.outbox = outbox
        self.responder = responder
        self.last_error: str | None = None

    @staticmethod
    def enabled() -> bool:
        return bool(settings.get("telegram_enabled") and settings.get("telegram_commands")
                    and settings.get("telegram_token") and settings.chat_ids())

    async def handle_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not chat_id or chat_id not in settings.chat_ids():
            return False
        command = parse_command(message.get("text") or "")
        if command is None:
            return False
        for text in await self.responder(command):
            await self.outbox.enqueue("reply", text, now_ts(), chat_ids=[chat_id])
        return True

    async def _skip_backlog(self) -> int:
        """First start: acknowledge old updates instead of answering them."""
        result = await self.sender.call("getUpdates", {"offset": -1, "timeout": 0})
        updates = result.result if result.ok and isinstance(result.result, list) else []
        offset = updates[-1]["update_id"] + 1 if updates else 1
        await run_db(settings.set, "telegram_update_offset", offset, True)
        return offset

    async def poll_once(self) -> bool:
        offset = int(settings.get("telegram_update_offset") or 0)
        if offset <= 0:
            offset = await self._skip_backlog()
        result = await self.sender.call(
            "getUpdates", {"offset": offset, "timeout": LONG_POLL, "allowed_updates": ["message", "channel_post"]},
            read_timeout=LONG_POLL + 15,
        )
        if not result.ok:
            self.last_error = result.error
            if result.error and "Conflict" in result.error:
                log.warning("getUpdates conflict: a webhook is set or another bot instance is polling")
            return False
        self.last_error = None
        updates = result.result or []
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            try:
                await self.handle_update(update)
            except Exception:
                log.exception("bot command failed")
        if updates:
            await run_db(settings.set, "telegram_update_offset", offset, True)
        return True

    async def run_forever(self) -> None:
        delay = 5.0
        while True:
            if not self.enabled():
                await asyncio.sleep(20)
                continue
            try:
                ok = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bot polling failed")
                ok = False
            if ok:
                delay = 5.0
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
