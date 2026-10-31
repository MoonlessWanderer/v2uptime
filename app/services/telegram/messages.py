"""English Telegram messages (parse_mode=HTML).

Every dynamic value goes through :func:`e` (``html.escape``); a server named
``A<B & C`` must never break message parsing. Names are wrapped in Unicode
isolates so a right-to-left name keeps its order. Telegram does not accept
``bdi`` or ``dir``. No message contains share links, addresses, or ports.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.services.checker import error_label
from app.services.settings import settings
from app.services.status import STATUS_EMOJI, STATUS_LABELS
from app.timeutil import fmt_date, fmt_datetime, fmt_duration, fmt_ms, fmt_percent, fmt_time, tz_label

MAX_LEN = 4096
SAFE_LEN = 4000  # margin under Telegram's 4096 limit
SEPARATOR = "\n\n-----\n\n"
FSI = "\u2068"
PDI = "\u2069"


def e(value: object) -> str:
    return html.escape(str(value), quote=False)


def bidi(value: object) -> str:
    """Isolate a name so right-to-left text does not reorder the sentence."""
    return f"{FSI}{e(value)}{PDI}"


def t(ts: int | None) -> str:
    if ts is None:
        return "—"
    return f"{fmt_datetime(ts, settings.tz)} ({tz_label(settings.tz, ts)})"


def clock(ts: int | None) -> str:
    return fmt_time(ts, settings.tz)


def span(start: int, end: int) -> str:
    """``18:28 → 18:40`` within one local day; with dates when the range crosses days."""
    zone = settings.tz
    if fmt_date(start, zone) == fmt_date(end, zone):
        return f"{clock(start)} → {clock(end)}"
    return f"{fmt_datetime(start, zone)} → {fmt_datetime(end, zone)}"


def ms(value: float | None) -> str:
    return fmt_ms(value)


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def truncate(text: str, limit: int) -> str:
    """Fit one HTML fragment into ``limit`` chars without breaking tags."""
    if len(text) <= limit:
        return text
    plain = e(strip_tags(text))
    return plain[: max(0, limit - 1)] + "…"


def split_message(parts: list[str], header: str = "", separator: str = "\n", limit: int = SAFE_LEN) -> list[str]:
    """Join self-contained HTML parts into as few messages as possible (≤ limit)."""
    return [text for text, _ in split_indexed(parts, header, separator, limit)]


def split_indexed(parts: list[str], header: str = "", separator: str = "\n",
                  limit: int = SAFE_LEN) -> list[tuple[str, list[int]]]:
    """Like :func:`split_message` but also returns which part indexes each chunk holds."""
    room = limit - len(header)
    chunks: list[tuple[str, list[int]]] = []
    body, indexes = "", []
    for index, part in enumerate(parts):
        part = truncate(part, room)
        candidate = part if not body else body + separator + part
        if body and len(candidate) > room:
            chunks.append((header + body, indexes))
            body, indexes = part, [index]
        else:
            body = candidate
            indexes.append(index)
    if body or not chunks:
        chunks.append((header + body, indexes))
    return chunks


# ----------------------------------------------------------------- events

def server_down(name: str, started_at: int, error_type: str | None, uptime_24h: float | None) -> str:
    return (f"🔴 <b>Server down: {bidi(name)}</b>\n"
            f"Since: {t(started_at)}\n"
            f"Error: {e(error_label(error_type))}\n"
            f"Uptime (24h): {fmt_percent(uptime_24h)}")


def server_up(name: str, started_at: int, ended_at: int, duration: int | None) -> str:
    return (f"🟢 <b>Server back online: {bidi(name)}</b>\n"
            f"Downtime: {fmt_duration(duration)} ({span(started_at, ended_at)})")


def flapping(name: str, count: int, window_minutes: int, at: int) -> str:
    return (f"⚠️ <b>Flapping: {bidi(name)}</b>\n"
            f"Changed {count} times in the last {window_minutes}m.\n"
            f"At: {t(at)}\n\n"
            f"Down and up alerts for this server are paused until it settles.")


def flap_end(name: str, in_incident: bool, at: int) -> str:
    state = "Down" if in_incident else "Online"
    return (f"✅ <b>Flapping ended: {bidi(name)}</b>\n"
            f"Current status: {state}\n"
            f"At: {t(at)}")


def network_down(at: int, servers: int) -> str:
    return (f"🚨 <b>Monitor network outage</b>\n"
            f"All {servers} servers and the reference check failed together. "
            f"The monitor's own internet is down.\n"
            f"Since: {t(at)}\n\n"
            f"Checks in this window are excluded from uptime.")


def network_up(started_at: int, ended_at: int) -> str:
    return (f"🌐 <b>Monitor network restored</b>\n"
            f"Downtime: <b>{fmt_duration(ended_at - started_at)}</b> ({span(started_at, ended_at)})")


def xray_crash(at: int, code: int | None, detail: str) -> str:
    text = (f"⚙️ <b>Xray crashed</b>\n"
            f"The Xray process exited (code {e(code)}) and is restarting.\n"
            f"{t(at)}")
    if detail:
        text += f"\n<code>{e(detail[:300])}</code>"
    return text


def xray_restarted(at: int, version: str | None) -> str:
    return f"⚙️ <b>Xray restarted</b>\nVersion: {e(version or '—')}\n{t(at)}"


def panel_started(at: int, version: str, servers: int) -> str:
    return (f"⚙️ <b>Monitor started</b>\n"
            f"Version: {e(version)}\n"
            f"Active servers: {servers}\n"
            f"{t(at)}")


def login_lock_alert(username: str) -> str:
    return (f'🔐 Many failed admin logins for "{e(username)}" from several IPs. '
            "Sign-ins from IPs that recently signed in still work.")


def test_message(at: int, via: str) -> str:
    location = settings.get("test_location")
    lines = [f"✅ <b>Test message</b>", "",
             f"The {bidi(settings.get('site_title'))} bot is configured."]
    if location:
        lines.append(f"Test location: {bidi(location)}")
    lines.append(f"Route: {e(via)}")
    lines.append(t(at))
    return "\n".join(lines)


def delay_note(created_at: int, now: int) -> str:
    if now - created_at < 120:
        return ""
    return f"\n\n<i>Delivered {fmt_duration(now - created_at)} late.</i>"


# ---------------------------------------------------------------- reports

@dataclass
class ReportRow:
    name: str
    uptime: float | None
    incidents: int
    downtime: int
    avg_latency: float | None


def report(title: str, period_label: str, rows: list[ReportRow], fastest: tuple[str, float] | None,
           stable: tuple[str, float] | None, at: int) -> list[str]:
    emoji = "📈" if "week" in title.lower() else "📊"
    location = settings.get("test_location")
    where = f"{bidi(location)} · " if location else ""
    header = (f"{emoji} <b>{e(title)}</b>\n"
              f"{where}{t(at)}\n"
              f"Range: {e(period_label)}\n\n")
    parts = []
    for row in rows:
        downtime = fmt_duration(row.downtime) if row.incidents else "0s"
        parts.append(
            f"{bidi(row.name)} — {fmt_percent(row.uptime)} · {ms(row.avg_latency)} · "
            f"{row.incidents} incidents ({downtime})"
        )
    if not parts:
        parts.append("No active servers.")
    footer = []
    if fastest:
        footer.append(f"Fastest: <b>{bidi(fastest[0])}</b> ({ms(fastest[1])})")
    if stable:
        footer.append(f"Most stable: <b>{bidi(stable[0])}</b> (score {stable[1]})")
    if footer:
        parts.append("\n".join(footer))
    return split_message(parts, header, separator="\n\n")


def status_overview(rows: list[tuple[str, str, int | None, float | None]], at: int) -> list[str]:
    """rows: (name, status, latency_ms, uptime_24h)."""
    header = f"📡 <b>Live status</b>\n{t(at)}\n\n"
    parts = [
        f"{STATUS_EMOJI[status]} <b>{bidi(name)}</b> — {STATUS_LABELS[status]} · {ms(latency)} · {fmt_percent(uptime)}"
        for name, status, latency, uptime in rows
    ] or ["No active servers."]
    return split_message(parts, header)


def incidents_overview(rows: list[tuple[str, int, int | None, str | None]], at: int) -> list[str]:
    """rows: (name, started_at, ended_at, error_type), newest first."""
    header = f"🧾 <b>Recent incidents</b>\n{t(at)}\n\n"
    parts = []
    for name, started, ended, error in rows:
        duration = fmt_duration((ended or at) - started)
        state = f"ongoing ({duration})" if ended is None else f"lasted {duration}"
        parts.append(f"<b>{bidi(name)}</b> · {t(started)} · {state} · {e(error_label(error))}")
    return split_message(parts or ["No incidents recorded."], header, separator="\n\n")


def help_text() -> str:
    return ("🤖 <b>Bot commands</b>\n\n"
            "/status — live status of every server\n"
            "/report — report for the last 24 hours\n"
            "/incidents — the 10 most recent incidents")


def merged_header(count: int) -> str:
    return f"📦 <b>Summary of {count} delayed messages</b>\n\n"
