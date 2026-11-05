import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.models import TelegramOutbox
from app.services import events as ev
from app.services.proxy_pool import ProxyCandidate
from app.services.settings import settings
from app.services.telegram import messages
from app.services.telegram.bot_commands import BotPoller, parse_command
from app.services.telegram.notifier import TelegramNotifier
from app.services.telegram.outbox_worker import (
    OutboxWorker,
    backoff_seconds,
    build_batches,
    enqueue_rows,
)
from app.services.telegram.sender import TelegramSender

from tests.conftest import make_server

TOKEN = "123456789:AAEhBP0av18bQqyCCpKQEgdA7pXy0pyu123"


class FakePool:
    def __init__(self, ids):
        self.ids = ids
        self.calls = []

    async def acandidates(self, exclude=(), preferred=None):
        self.calls.append(set(exclude))
        return [ProxyCandidate(i, f"srv{i}", 20000 + i, "online", 90, 100) for i in self.ids if i not in set(exclude)]


class FakeTelegram:
    """Routes calls by proxy URL: behaviour per route is 'ok', 'down', '429' or 'reject'."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = []

    def factory(self, proxy, timeout):
        route = proxy or "direct"

        def handler(request):
            self.requests.append((route, request))
            mode = self.behaviour.get(route, "ok")
            if mode == "down":
                raise httpx.ConnectError("blocked")
            if mode == "429":
                return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                                 "parameters": {"retry_after": 7}})
            if mode == "reject":
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)


@pytest.fixture
def tg(database):
    settings.set_many({"telegram_enabled": True, "telegram_token": TOKEN, "telegram_chat_ids": "111,-100222",
                       "telegram_send_mode": "auto"})
    return database


def run(coro):
    return asyncio.run(coro)


def sender_with(behaviour, pool_ids=(1, 2, 3, 4, 5)):
    fake = FakeTelegram(behaviour)
    pool = FakePool(list(pool_ids))
    return TelegramSender(pool, client_factory=fake.factory, api_base="https://api.test"), fake, pool


# ----------------------------------------------------------------- sender

def test_falls_back_from_direct_to_servers_and_caches_route(tg):
    sender, fake, _ = sender_with({"direct": "down", "socks5h://127.0.0.1:20001": "down"})
    result = run(sender.call("sendMessage", {"chat_id": "1", "text": "x"}))
    assert result.ok and result.route.server_id == 2
    assert [r for r, _ in fake.requests] == ["direct", "socks5h://127.0.0.1:20001", "socks5h://127.0.0.1:20002"]
    fake.requests.clear()
    run(sender.call("sendMessage", {"chat_id": "1", "text": "x"}))
    assert fake.requests[0][0] == "socks5h://127.0.0.1:20002"  # cached route first


def test_tries_at_most_four_servers(tg):
    behaviour = {"direct": "down", **{f"socks5h://127.0.0.1:{20000 + i}": "down" for i in range(1, 6)}}
    sender, fake, _ = sender_with(behaviour)
    result = run(sender.call("sendMessage", {}))
    assert not result.ok and len(fake.requests) == 1 + 4


def test_subject_server_is_excluded(tg):
    sender, fake, pool = sender_with({"direct": "down"})
    result = run(sender.call("sendMessage", {}, exclude=[1]))
    assert pool.calls[-1] == {1} and result.route.server_id == 2


def test_direct_only_and_via_server_modes(tg):
    settings.set("telegram_send_mode", "via_server")
    sender, fake, _ = sender_with({})
    run(sender.call("sendMessage", {}))
    assert fake.requests[0][0].startswith("socks5h://")
    settings.set("telegram_send_mode", "direct")
    sender, fake, _ = sender_with({"direct": "down"})
    assert not run(sender.call("sendMessage", {})).ok and len(fake.requests) == 1


def test_rate_limit_and_rejection_do_not_try_other_routes(tg):
    sender, fake, _ = sender_with({"direct": "429"})
    result = run(sender.call("sendMessage", {}))
    assert result.retry_after == 7 and len(fake.requests) == 1
    sender, fake, _ = sender_with({"direct": "reject"})
    result = run(sender.call("sendMessage", {}))
    assert result.permanent and len(fake.requests) == 1


def test_token_never_in_error(tg):
    sender, fake, _ = sender_with({"direct": "down"}, pool_ids=())
    result = run(sender.call("sendMessage", {}))
    assert TOKEN not in (result.error or "")


# ----------------------------------------------------------------- outbox

def test_queue_keeps_messages_when_no_route_and_backs_off(tg):
    sender, fake, _ = sender_with({"direct": "down"}, pool_ids=())
    worker = OutboxWorker(sender)
    enqueue_rows("down", "msg", 1000, ["111"])
    assert run(worker.process_once(now=2000)) == 0
    row = TelegramOutbox.get()
    assert row.status == "pending" and row.attempts == 1 and row.next_attempt_at == 2000 + 30
    assert run(worker.process_once(now=2010)) == 0  # not due yet
    run(worker.process_once(now=2031))
    assert TelegramOutbox.get().next_attempt_at == 2031 + 60
    assert [backoff_seconds(n) for n in (1, 2, 3, 6, 10)] == [30, 60, 120, 600, 600]


def test_queue_sends_when_route_returns(tg):
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    enqueue_rows("down", "hello", 1000, ["111", "-100222"])
    assert run(worker.process_once(now=1500)) == 2
    rows = list(TelegramOutbox.select())
    assert all(r.status == "sent" and r.via == "direct" for r in rows)
    assert "late" in json.loads(fake.requests[0][1].read())["text"]  # 500s late -> delay note


def test_more_than_ten_messages_are_merged(tg):
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    for i in range(12):
        enqueue_rows("down", f"event {i}", 1000 + i, ["111"])
    assert run(worker.process_once(now=1100)) == 1
    body = fake.requests[0][1].read().decode()
    assert "Summary" in body
    assert TelegramOutbox.select().where(TelegramOutbox.status == "sent").count() == 12


def test_fresh_summary_is_not_called_delayed():
    rows = [TelegramOutbox(id=i, text=f"event {i}", created_at=1000, exclude_server_id=None) for i in range(11)]
    [batch] = build_batches(rows, now=1060)
    assert "Summary of 11 messages" in batch.text
    assert "delayed" not in batch.text and "late" not in batch.text


def test_late_summary_says_how_late_its_oldest_message_is():
    rows = [TelegramOutbox(id=i, text=f"event {i}", created_at=1000 + i, exclude_server_id=None) for i in range(11)]
    [batch] = build_batches(rows, now=1000 + 8 * 3600)
    assert "Summary of 11 delayed messages" in batch.text
    assert batch.text.endswith("<i>Oldest message delivered 8h late.</i>")


def test_old_alert_in_backoff_goes_out_before_a_newer_message(tg):
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    enqueue_rows("network_down", "old alert", 1000, ["111"])
    TelegramOutbox.update(attempts=50, next_attempt_at=9600).execute()
    enqueue_rows("panel_started", "new message", 9000, ["111"])
    assert run(worker.process_once(now=9001)) == 2
    texts = [json.loads(request[1].read())["text"] for request in fake.requests]
    assert texts[0].startswith("old alert") and "late" in texts[0]
    assert texts[1] == "new message"


def test_backoff_still_waits_when_nothing_else_is_due(tg):
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    enqueue_rows("network_down", "old alert", 1000, ["111"])
    TelegramOutbox.update(next_attempt_at=9600).execute()
    assert run(worker.process_once(now=9001)) == 0
    assert fake.requests == []


def test_a_due_row_does_not_pull_another_chats_backoff(tg):
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    enqueue_rows("down", "other chat", 1000, ["222"])
    TelegramOutbox.update(next_attempt_at=9600).execute()
    enqueue_rows("down", "this chat", 9000, ["111"])
    assert run(worker.process_once(now=9001)) == 1
    assert json.loads(fake.requests[0][1].read())["text"] == "this chat"


def test_exit_code_zero_is_not_called_a_crash(database):
    stopped = messages.xray_crash(1_700_000_000, 0, "a log line")
    assert "Xray stopped" in stopped and "crashed" not in stopped
    assert "Last log line:" in stopped and "a log line" in stopped
    assert "Xray crashed" in messages.xray_crash(1_700_000_000, 1, "")


def test_merged_messages_respect_telegram_limit():
    rows = [TelegramOutbox(id=i, text=("x" * 900) + f" #{i}", created_at=0, exclude_server_id=None) for i in range(30)]
    batches = build_batches(rows, now=10)
    assert len(batches) > 1
    assert all(len(b.text) <= messages.MAX_LEN for b in batches)
    assert sorted(i for b in batches for i in b.ids) == list(range(30))


def test_rate_limit_pauses_queue(tg):
    sender, fake, _ = sender_with({"direct": "429"})
    worker = OutboxWorker(sender)
    enqueue_rows("down", "a", 1000, ["111"])
    run(worker.process_once(now=1000))
    assert worker.paused_until == 1007 and TelegramOutbox.get().next_attempt_at == 1007


def test_rejected_message_fails_after_retries(tg):
    sender, fake, _ = sender_with({"direct": "reject"})
    worker = OutboxWorker(sender)
    enqueue_rows("down", "a", 1000, ["111"])
    now = 1000
    for _ in range(5):
        run(worker.process_once(now=now))
        now += 1000
    assert TelegramOutbox.get().status == "failed"


# --------------------------------------------------------------- messages

def test_messages_escape_html():
    text = messages.server_down("<b>A&B</b>", 1000, "timeout", 99.5)
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in text and "<b>A&B</b>" not in text
    assert "99.5%" in text


def test_split_message():
    parts = [f"line {i} " + "y" * 300 for i in range(40)]
    chunks = messages.split_message(parts, header="H\n")
    assert all(len(c) <= messages.SAFE_LEN and c.startswith("H\n") for c in chunks)
    assert sum(c.count("line ") for c in chunks) == 40
    assert len(messages.split_message(["<b>" + "z" * 5000 + "</b>"])[0]) <= messages.SAFE_LEN


# ---------------------------------------------------------- notifier/bot

def test_down_event_is_queued_excluding_its_server(tg):
    server = make_server("Srv & Co")
    sender, fake, _ = sender_with({})
    worker = OutboxWorker(sender)
    notifier = TelegramNotifier(worker, sender, stats=None)

    async def scenario():
        notifier.handle_event(ev.Event(ev.DOWN, 1000, server.id, {"started_at": 900, "error_type": "timeout"}))
        await asyncio.gather(*notifier._tasks)

    run(scenario())
    rows = list(TelegramOutbox.select())
    assert len(rows) == 2 and all(r.exclude_server_id == server.id for r in rows)
    assert "Srv &amp; Co" in rows[0].text


def test_disabled_categories_are_not_queued(tg):
    settings.set("telegram_events", "up")
    notifier = TelegramNotifier(OutboxWorker(sender_with({})[0]), None, None)
    assert notifier.wants("up") and not notifier.wants("down")


def test_bot_answers_only_registered_chats(tg):
    async def responder(command):
        return [f"answer {command}"]

    poller = BotPoller(sender_with({})[0], OutboxWorker(sender_with({})[0]), responder)
    assert run(poller.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}})) is False
    assert run(poller.handle_update({"message": {"chat": {"id": 111}, "text": "/status@MyBot"}})) is True
    rows = list(TelegramOutbox.select())
    assert len(rows) == 1 and rows[0].chat_id == "111" and rows[0].text == "answer /status"
    assert parse_command("hello") is None and parse_command("/Report extra") == "/report"


def test_daily_report_schedule(tg, monkeypatch):
    sent = []
    notifier = TelegramNotifier(OutboxWorker(sender_with({})[0]), None, None)
    monkeypatch.setattr(notifier, "build_report", lambda days, now=None: [f"report {days}"])

    async def fake_enqueue(kind, text, at=None, **kw):
        sent.append((kind, text))

    monkeypatch.setattr(notifier.outbox, "enqueue", fake_enqueue)
    settings.set_many({"telegram_daily_time": "09:00", "telegram_weekly_day": "5", "timezone": "Asia/Tehran"})
    base = datetime(2025, 11, 1, 8, 0, tzinfo=ZoneInfo("Asia/Tehran"))  # a Saturday
    t8 = int(base.timestamp())
    assert run(notifier.maybe_send_reports(t8)) == []  # first run only initialises
    settings.set_many({"last_daily_report": "2025-10-31", "last_weekly_report": "2025-10-25"}, validated=True)
    assert run(notifier.maybe_send_reports(t8)) == []  # before 09:00
    assert run(notifier.maybe_send_reports(t8 + 3600)) == ["daily", "weekly"]
    assert run(notifier.maybe_send_reports(t8 + 7200)) == []  # once per day
    assert sent == [("daily", "report 1"), ("weekly", "report 7")]


def _utc(*args):
    return int(datetime(*args, tzinfo=ZoneInfo("UTC")).timestamp())


def test_recovery_range_same_day_shows_clock_only(database):
    start = _utc(2026, 10, 31, 14, 58)  # 18:28 in Asia/Tehran
    assert "Downtime: 12m (18:28 → 18:40)" in messages.server_up("S", start, start + 720, 720)
    assert "Downtime: <b>12m</b> (18:28 → 18:40)" in messages.network_up(start, start + 720)


def test_recovery_range_across_days_shows_dates(database):
    start, end = _utc(2026, 10, 30, 14, 58), _utc(2026, 10, 31, 17, 10)  # 18:28 -> next day 20:40 local
    text = messages.server_up("S", start, end, end - start)
    assert "Downtime: 1d 2h (2026-10-30 18:28 → 2026-10-31 20:40)" in text
    assert "(2026-10-30 18:28 → 2026-10-31 20:40)" in messages.network_up(start, end)
