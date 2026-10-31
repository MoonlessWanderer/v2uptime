"""Persian Telegram messages (parse_mode=HTML).

Every dynamic value goes through :func:`e` (``html.escape``); a server named
``A<B & C`` must never break message parsing.  No message contains share
links, addresses or ports.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.services.checker import error_label
from app.services.settings import settings
from app.services.status import STATUS_EMOJI, STATUS_LABELS
from app.timeutil import fa, fmt_datetime, fmt_duration, fmt_percent

MAX_LEN = 4096
SAFE_LEN = 4000  # margin under Telegram's 4096 limit
SEPARATOR = "\n\n➖➖➖➖➖\n\n"


def e(value: object) -> str:
    return html.escape(str(value), quote=False)


def t(ts: int | None) -> str:
    return fmt_datetime(ts, settings.tz, settings.jalali)


def ms(value: float | None) -> str:
    return "—" if value is None else fa(f"{int(round(value))} ms")


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
    return (f"🔴 <b>قطع شدن سرور</b>\n\n"
            f"🖥 سرور: <b>{e(name)}</b>\n"
            f"🕒 شروع قطعی: {t(started_at)}\n"
            f"⚠️ نوع خطا: {e(error_label(error_type))}\n"
            f"📊 آپتایم ۲۴ ساعت: {fmt_percent(uptime_24h)}")


def server_up(name: str, started_at: int, ended_at: int, duration: int | None) -> str:
    return (f"🟢 <b>وصل شدن دوباره سرور</b>\n\n"
            f"🖥 سرور: <b>{e(name)}</b>\n"
            f"⏳ مدت قطعی: <b>{fmt_duration(duration)}</b>\n"
            f"🕒 شروع: {t(started_at)}\n"
            f"🕘 پایان: {t(ended_at)}")


def flapping(name: str, count: int, window_minutes: int, at: int) -> str:
    return (f"⚠️ <b>نوسان (Flapping)</b>\n\n"
            f"🖥 سرور: <b>{e(name)}</b>\n"
            f"در {fa(window_minutes)} دقیقه اخیر {fa(count)} بار قطع و وصل شده است.\n"
            f"🕒 زمان: {t(at)}\n\n"
            f"تا پایدار شدن وضعیت، پیام قطع/وصل این سرور ارسال نمی‌شود.")


def flap_end(name: str, in_incident: bool, at: int) -> str:
    state = "🔴 قطع" if in_incident else "🟢 وصل"
    return (f"✅ <b>پایان نوسان</b>\n\n"
            f"🖥 سرور: <b>{e(name)}</b>\n"
            f"وضعیت فعلی: {state}\n"
            f"🕒 زمان: {t(at)}")


def network_down(at: int, servers: int) -> str:
    return (f"🚨 <b>قطعی شبکه سرور مانیتور</b>\n\n"
            f"همه {fa(servers)} سرور و چک مرجع داخلی هم‌زمان ناموفق بودند؛ اینترنت خود سرور مانیتور قطع است.\n"
            f"🕒 شروع: {t(at)}\n\n"
            f"نتایج این بازه در آمار آپتایم حساب نمی‌شوند.")


def network_up(started_at: int, ended_at: int) -> str:
    return (f"🌐 <b>برگشت شبکه سرور مانیتور</b>\n\n"
            f"⏳ مدت قطعی: <b>{fmt_duration(ended_at - started_at)}</b>\n"
            f"🕒 شروع: {t(started_at)}\n"
            f"🕘 پایان: {t(ended_at)}")


def xray_crash(at: int, code: int | None, detail: str) -> str:
    text = f"⚙️ <b>کرش Xray</b>\n\nپروسه Xray متوقف شد (کد خروج {e(code)}) و در حال راه‌اندازی مجدد است.\n🕒 {t(at)}"
    if detail:
        text += f"\n<code>{e(detail[:300])}</code>"
    return text


def xray_restarted(at: int, version: str | None) -> str:
    return f"⚙️ <b>Xray دوباره راه‌اندازی شد</b>\n\nنسخه: {e(version or '—')}\n🕒 {t(at)}"


def panel_started(at: int, version: str, servers: int) -> str:
    return (f"⚙️ <b>پنل مانیتورینگ شروع به کار کرد</b>\n\n"
            f"نسخه پنل: {e(version)}\n"
            f"تعداد سرورهای فعال: {fa(servers)}\n"
            f"🕒 {t(at)}")


def test_message(at: int, via: str) -> str:
    location = settings.get("test_location")
    return (f"✅ <b>پیام تست</b>\n\n"
            f"ربات «{e(settings.get('site_title'))}» به‌درستی تنظیم شده است.\n"
            f"📍 محل تست: {e(location)}\n"
            f"🛰 مسیر ارسال: {e(via)}\n"
            f"🕒 {t(at)}")


def delay_note(created_at: int, now: int) -> str:
    if now - created_at < 120:
        return ""
    return f"\n\n<i>⏱ این پیام با {fmt_duration(now - created_at)} تأخیر ارسال شد.</i>"


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
    emoji = "📊" if "روزانه" in title else "📈"
    header = (f"{emoji} <b>{e(title)}</b>\n"
              f"📍 {e(settings.get('test_location'))} · {t(at)}\n"
              f"بازه: {e(period_label)}\n\n")
    parts = []
    for row in rows:
        parts.append(
            f"🖥 <b>{e(row.name)}</b>\n"
            f"   آپتایم: {fmt_percent(row.uptime)} · پینگ: {ms(row.avg_latency)}\n"
            f"   قطعی: {fa(row.incidents)} بار ({fmt_duration(row.downtime) if row.incidents else '۰'})"
        )
    if not parts:
        parts.append("هیچ سرور فعالی وجود ندارد.")
    footer = []
    if fastest:
        footer.append(f"⚡️ سریع‌ترین: <b>{e(fastest[0])}</b> ({ms(fastest[1])})")
    if stable:
        footer.append(f"🛡 پایدارترین: <b>{e(stable[0])}</b> (امتیاز {fa(stable[1])})")
    if footer:
        parts.append("\n".join(footer))
    return split_message(parts, header, separator="\n\n")


def status_overview(rows: list[tuple[str, str, int | None, float | None]], at: int) -> list[str]:
    """rows: (name, status, latency_ms, uptime_24h)."""
    header = f"📡 <b>وضعیت لحظه‌ای سرورها</b>\n🕒 {t(at)}\n\n"
    parts = [
        f"{STATUS_EMOJI[status]} <b>{e(name)}</b> — {STATUS_LABELS[status]} · {ms(latency)} · {fmt_percent(uptime)}"
        for name, status, latency, uptime in rows
    ] or ["هیچ سرور فعالی وجود ندارد."]
    return split_message(parts, header)


def incidents_overview(rows: list[tuple[str, int, int | None, str | None]], at: int) -> list[str]:
    """rows: (name, started_at, ended_at, error_type), newest first."""
    header = f"🧾 <b>آخرین قطعی‌ها</b>\n🕒 {t(at)}\n\n"
    parts = []
    for name, started, ended, error in rows:
        duration = fmt_duration((ended or at) - started)
        state = f"ادامه دارد ({duration})" if ended is None else f"مدت: {duration}"
        parts.append(f"🔻 <b>{e(name)}</b>\n   {t(started)} · {state} · {e(error_label(error))}")
    return split_message(parts or ["قطعی‌ای ثبت نشده است."], header, separator="\n\n")


def help_text() -> str:
    return ("🤖 <b>دستورهای ربات</b>\n\n"
            "/status — وضعیت لحظه‌ای همه سرورها\n"
            "/report — گزارش ۲۴ ساعت اخیر\n"
            "/incidents — آخرین ۱۰ قطعی")


def merged_header(count: int) -> str:
    return f"📦 <b>خلاصه {fa(count)} پیام عقب‌افتاده</b>\n\n"
