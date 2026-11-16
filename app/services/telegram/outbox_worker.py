"""Persistent Telegram queue.

Monitoring never waits for Telegram: messages are stored in
``TelegramOutbox`` (one row per chat) and a background worker sends them.

* No reachable route → rows stay pending and retry with backoff
  (30s doubling up to 10 minutes).  The text already contains the real event
  time; a delay note is appended when sent late.
* More than 10 pending rows for a chat are merged into summary message(s),
  each within Telegram's 4096-character limit.
* HTTP 429 pauses the whole queue for ``retry_after`` seconds.
* Rows Telegram rejects (bad chat id, ...) fail after 5 attempts.
* Sent/failed rows are deleted after 7 days by the aggregator cleanup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.db import db, run_db
from app.models import TelegramOutbox
from app.services.settings import settings
from app.services.telegram import messages
from app.services.telegram.sender import TelegramSender
from app.timeutil import DAY, now_ts

log = logging.getLogger(__name__)

MERGE_THRESHOLD = 10
MAX_PERMANENT_ATTEMPTS = 5
POLL_SECONDS = 5


def backoff_seconds(attempts: int) -> int:
    return min(30 * 2 ** max(0, attempts - 1), 600)


@dataclass
class Batch:
    text: str
    ids: list[int]
    exclude: set[int] = field(default_factory=set)


def build_batches(rows: list[TelegramOutbox], now: int) -> list[Batch]:
    """One message per row, or merged summaries when more than 10 are waiting."""
    if len(rows) <= MERGE_THRESHOLD:
        return [Batch(r.text + messages.delay_note(r.created_at, now), [r.id],
                      {r.exclude_server_id} if r.exclude_server_id else set()) for r in rows]
    late = bool(messages.delay_note(min(r.created_at for r in rows), now))
    header = messages.merged_header(len(rows), late)
    chunks = messages.split_indexed([r.text for r in rows], header, messages.SEPARATOR)
    batches = []
    for text, indexes in chunks:
        included = [rows[i] for i in indexes]
        note = messages.delay_note(min(r.created_at for r in included), now, "Oldest message delivered")
        batches.append(Batch(text + note, [r.id for r in included],
                             {r.exclude_server_id for r in included if r.exclude_server_id}))
    return batches


# ------------------------------------------------------------- DB helpers

def enqueue_rows(event_type: str, text: str, at: int, chat_ids: list[str],
                 exclude_server_id: int | None = None) -> int:
    rows = [{"event_type": event_type, "chat_id": chat, "text": text, "created_at": at, "next_attempt_at": 0,
             "exclude_server_id": exclude_server_id} for chat in chat_ids]
    if rows:
        TelegramOutbox.insert_many(rows).execute()
    return len(rows)


def due_rows(now: int) -> list[TelegramOutbox]:
    """Every pending row of each chat that has a due row, oldest first.

    A row still in backoff goes out with a newer due row, so an old alert is
    not delivered after the messages that followed it. A row that excludes a
    server keeps its own schedule: that server may be the only working route,
    and the failed send would hold back the newer rows behind it.
    """
    due_chats = (TelegramOutbox.select(TelegramOutbox.chat_id)
                 .where(TelegramOutbox.status == "pending", TelegramOutbox.next_attempt_at <= now))
    return list(TelegramOutbox.select()
                .where(TelegramOutbox.status == "pending", TelegramOutbox.chat_id.in_(due_chats),
                       (TelegramOutbox.next_attempt_at <= now) | TelegramOutbox.exclude_server_id.is_null())
                .order_by(TelegramOutbox.created_at, TelegramOutbox.id).limit(500))


def mark_sent(ids: list[int], now: int, via: str) -> None:
    TelegramOutbox.update(status="sent", sent_at=now, via=via[:200], last_error=None,
                          attempts=TelegramOutbox.attempts + 1).where(TelegramOutbox.id.in_(ids)).execute()


def mark_retry(ids: list[int], now: int, error: str, delay: int | None = None) -> None:
    with db.atomic():
        for row in TelegramOutbox.select().where(TelegramOutbox.id.in_(ids)):
            row.attempts += 1
            row.last_error = error[:500]
            row.next_attempt_at = now + (delay if delay is not None else backoff_seconds(row.attempts))
            row.save()


def mark_rejected(ids: list[int], now: int, error: str) -> None:
    with db.atomic():
        for row in TelegramOutbox.select().where(TelegramOutbox.id.in_(ids)):
            row.attempts += 1
            row.last_error = error[:500]
            if row.attempts >= MAX_PERMANENT_ATTEMPTS:
                row.status = "failed"
            row.next_attempt_at = now + backoff_seconds(row.attempts)
            row.save()


def queue_stats() -> dict[str, object]:
    now = now_ts()
    pending = TelegramOutbox.select().where(TelegramOutbox.status == "pending")
    last_failed = (TelegramOutbox.select().where(TelegramOutbox.last_error.is_null(False))
                   .order_by(TelegramOutbox.id.desc()).first())
    last_sent = (TelegramOutbox.select().where(TelegramOutbox.status == "sent")
                 .order_by(TelegramOutbox.sent_at.desc()).first())
    return {
        "pending": pending.count(),
        "oldest_pending": pending.order_by(TelegramOutbox.created_at).first().created_at if pending.exists() else None,
        "failed": TelegramOutbox.select().where(TelegramOutbox.status == "failed").count(),
        "sent_24h": TelegramOutbox.select().where(TelegramOutbox.status == "sent",
                                                  TelegramOutbox.sent_at >= now - DAY).count(),
        "last_error": last_failed.last_error if last_failed else None,
        "last_error_at": last_failed.created_at if last_failed else None,
        "last_sent_at": last_sent.sent_at if last_sent else None,
        "last_via": last_sent.via if last_sent else None,
    }


# ----------------------------------------------------------------- worker

class OutboxWorker:
    def __init__(self, sender: TelegramSender) -> None:
        self.sender = sender
        self.paused_until = 0
        self.last_error: str | None = None
        self._wake: asyncio.Event | None = None

    def _wake_event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    async def enqueue(self, event_type: str, text: str, at: int | None = None,
                      chat_ids: list[str] | None = None, exclude_server_id: int | None = None) -> int:
        chats = chat_ids if chat_ids is not None else settings.chat_ids()
        count = await run_db(enqueue_rows, event_type, text, at or now_ts(), chats, exclude_server_id)
        self._wake_event().set()
        return count

    async def process_once(self, now: int | None = None) -> int:
        """Send what is due.  Returns the number of Telegram messages sent."""
        now = now or now_ts()
        if not settings.get("telegram_enabled") or not settings.get("telegram_token"):
            return 0
        if now < self.paused_until:
            return 0
        rows = await run_db(due_rows, now)
        if not rows:
            return 0
        by_chat: dict[str, list[TelegramOutbox]] = {}
        for row in rows:
            by_chat.setdefault(row.chat_id, []).append(row)
        pending_ids = {row.id for row in rows}
        sent = 0
        for chat_id, chat_rows in by_chat.items():
            for batch in build_batches(chat_rows, now):
                result = await self.sender.call("sendMessage", {
                    "chat_id": chat_id, "text": batch.text, "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, exclude=batch.exclude)
                if result.ok:
                    await run_db(mark_sent, batch.ids, now, result.route.label if result.route else "")
                    pending_ids -= set(batch.ids)
                    sent += 1
                    self.last_error = None
                    await asyncio.sleep(0.05)
                    continue
                self.last_error = result.error
                if result.retry_after:
                    self.paused_until = now + result.retry_after
                    await run_db(mark_retry, sorted(pending_ids), now, result.error or "429", result.retry_after)
                    log.warning("telegram rate limited", extra={"retry_after": result.retry_after})
                    return sent
                if result.permanent:
                    await run_db(mark_rejected, batch.ids, now, result.error or "rejected")
                    pending_ids -= set(batch.ids)
                    log.warning("telegram rejected message", extra={"chat": chat_id, "error": result.error})
                    continue
                # No route reached Telegram: back off everything that is due.
                await run_db(mark_retry, sorted(pending_ids), now, result.error or "send failed")
                log.warning("telegram unreachable, will retry", extra={"error": (result.error or "")[:300]})
                return sent
        return sent

    async def run_forever(self) -> None:
        wake = self._wake_event()
        while True:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox processing failed")
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
