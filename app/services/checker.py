"""Connection test engine.

Every ``check_interval`` seconds each server loaded in Xray is tested by an
HTTP request through its local SOCKS inbound (``socks5h``: DNS is resolved
through the tunnel).  A failed test is retried once immediately; only the
final result is recorded.  A direct "reference" request to a domestic site
detects when the monitor's own internet is down: if every server *and* the
reference fail together, the cycle's results are stored as ``unknown``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from app.models import STATUS_FAIL, STATUS_OK, STATUS_UNKNOWN
from app.services import net
from app.services.settings import settings
from app.services.xray_manager import XrayManager
from app.timeutil import now_ts

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# error_type values
E_TIMEOUT = "timeout"
E_REFUSED = "connection_refused"
E_RESET = "connection_reset"
E_TLS = "tls"
E_DNS = "dns"
E_HTTP = "http_status"
E_PROXY = "proxy"
E_NETWORK = "network"
E_XRAY = "xray_unavailable"
E_MONITOR_NET = "monitor_network"
E_UNKNOWN = "unknown"

ERROR_LABELS: dict[str, str] = {
    E_TIMEOUT: "Timeout",
    E_REFUSED: "Connection refused",
    E_RESET: "Connection reset",
    E_TLS: "TLS error",
    E_DNS: "DNS error",
    E_HTTP: "Invalid HTTP status",
    E_PROXY: "Proxy error",
    E_NETWORK: "Network error",
    E_XRAY: "Xray unavailable",
    E_MONITOR_NET: "Monitor network outage",
    E_UNKNOWN: "Unknown error",
}


def error_label(error_type: str | None) -> str:
    return ERROR_LABELS.get(error_type or "", error_type or "—")


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: int | None = None
    error_type: str | None = None
    detail: str | None = None
    status_code: int | None = None


@dataclass
class CheckRecord:
    server_id: int
    checked_at: int
    status: str  # ok / fail / unknown
    latency_ms: int | None
    error_type: str | None
    error_detail: str | None = None


@dataclass
class CycleReport:
    started_at: int
    duration: float = 0.0
    checked: int = 0
    ok: int = 0
    fail: int = 0
    unknown: int = 0
    reference_ok: bool | None = None
    network_down: bool = False
    discarded: bool = False
    skipped_reason: str | None = None
    records: list[CheckRecord] = field(default_factory=list)


def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 5:
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def classify_exception(exc: BaseException, via_proxy: bool) -> tuple[str, str]:
    """Map an exception to (error_type, short detail)."""
    detail = _exception_chain_text(exc)[:300]
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return E_TIMEOUT, detail
    text = detail.lower()
    if "refused" in text:
        # Through SOCKS the only TCP connect is to our own Xray inbound.
        return (E_XRAY if via_proxy else E_REFUSED), detail
    if isinstance(exc, httpx.ProxyError):
        return E_PROXY, detail
    if any(k in text for k in ("certificate", "wrong version number", "handshake failure", "tlsv1 alert",
                               "sslv3 alert", "unknown protocol")):
        return E_TLS, detail
    if any(k in text for k in ("name or service not known", "getaddrinfo", "nodename nor servname",
                               "temporary failure in name resolution", "no address associated", "name resolution")):
        return E_DNS, detail
    if any(k in text for k in ("eof", "reset", "disconnected", "broken pipe", "connection aborted", "closed")):
        return E_RESET, detail
    if "ssl" in text or "tls" in text:
        return E_TLS, detail
    if isinstance(exc, (httpx.NetworkError, httpx.TransportError, OSError)):
        return E_NETWORK, detail
    return E_UNKNOWN, detail


async def probe(url: str, proxy: str | None, timeout: float, accept_any_status: bool = False) -> ProbeResult:
    """One HTTP GET; latency is the full request time (connect + TLS + response).

    The client is built before the timer starts, so client setup is never
    counted as network latency.
    """
    try:
        async with net.async_client(proxy, httpx.Timeout(timeout), follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT}) as client:
            async def _request() -> int:
                async with client.stream("GET", url) as response:
                    return response.status_code

            start = time.perf_counter()
            status = await asyncio.wait_for(_request(), timeout)
            latency = int((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001 - every failure is a result
        error_type, detail = classify_exception(exc, via_proxy=proxy is not None)
        return ProbeResult(False, None, error_type, detail)
    if (accept_any_status and status < 500) or 200 <= status < 300:
        return ProbeResult(True, latency, None, None, status)
    return ProbeResult(False, latency, E_HTTP, f"HTTP {status}", status)


def socks_url(port: int) -> str:
    return f"socks5h://127.0.0.1:{port}"


def apply_network_rule(results: dict[int, ProbeResult], reference_ok: bool) -> tuple[dict[int, tuple[str, ProbeResult]], bool]:
    """Decide final status per server.

    * Xray-side failures (local inbound refused) are ``unknown``.
    * If every real result failed *and* the direct reference check failed,
      the monitor itself is offline: all results become ``unknown``.
    Returns ({server_id: (status, result)}, network_down).
    """
    real = [r for r in results.values() if r.error_type != E_XRAY]
    network_down = bool(real) and all(not r.ok for r in real) and not reference_ok
    final: dict[int, tuple[str, ProbeResult]] = {}
    for server_id, result in results.items():
        if result.error_type == E_XRAY:
            final[server_id] = (STATUS_UNKNOWN, result)
        elif network_down:
            final[server_id] = (STATUS_UNKNOWN, ProbeResult(False, None, E_MONITOR_NET, result.detail))
        else:
            final[server_id] = (STATUS_OK if result.ok else STATUS_FAIL, result)
    return final, network_down


ResultHandler = Callable[[CycleReport], Awaitable[None]]


class Checker:
    def __init__(self, xray: XrayManager, on_cycle: ResultHandler) -> None:
        self.xray = xray
        self.on_cycle = on_cycle
        self.last_report: CycleReport | None = None
        self.running = False
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Start the next cycle early (e.g. after settings changed)."""
        self._wake.set()

    async def check_server(self, port: int, timeout: float, url: str) -> ProbeResult:
        result = await probe(url, socks_url(port), timeout)
        if not result.ok and result.error_type != E_XRAY:
            result = await probe(url, socks_url(port), timeout)  # one immediate retry
        return result

    async def test_now(self, server_id: int) -> ProbeResult:
        """Manual test from the admin panel; not recorded."""
        port = self.xray.loaded_ports().get(server_id)
        if port is None:
            return ProbeResult(False, None, E_XRAY, "This server is not loaded in Xray.")
        return await self.check_server(port, settings.get("check_timeout"), settings.get("test_url"))

    async def run_cycle(self) -> CycleReport:
        report = CycleReport(started_at=now_ts())
        started = time.monotonic()
        if not self.xray.installed:
            report.skipped_reason = "xray_missing"
            return report
        if not await self.xray.wait_ready(timeout=30):
            report.skipped_reason = "xray_not_ready"
            log.warning("skipping check cycle: xray not ready")
            return report
        epoch = self.xray.epoch
        targets = self.xray.loaded_ports()
        if not targets:
            report.skipped_reason = "no_servers"
            return report

        timeout = float(settings.get("check_timeout"))
        url = settings.get("test_url")
        semaphore = asyncio.Semaphore(int(settings.get("concurrency")))

        async def run_one(server_id: int, port: int) -> tuple[int, int, ProbeResult]:
            async with semaphore:
                checked_at = now_ts()
                return server_id, checked_at, await self.check_server(port, timeout, url)

        reference_task = asyncio.create_task(
            probe(settings.get("reference_url"), None, timeout, accept_any_status=True)
        )
        outcomes = await asyncio.gather(*(run_one(sid, port) for sid, port in targets.items()))
        reference = await reference_task
        report.duration = time.monotonic() - started

        if self.xray.epoch != epoch or not self.xray.is_ready():
            report.discarded = True
            log.warning("discarding check cycle: xray restarted during cycle")
            return report

        results = {sid: result for sid, _, result in outcomes}
        times = {sid: ts for sid, ts, _ in outcomes}
        final, network_down = apply_network_rule(results, reference.ok)
        report.reference_ok = reference.ok
        report.network_down = network_down
        for server_id, (status, result) in final.items():
            report.records.append(CheckRecord(
                server_id=server_id, checked_at=times[server_id], status=status,
                latency_ms=result.latency_ms if status == STATUS_OK else None,
                error_type=result.error_type, error_detail=result.detail,
            ))
        report.checked = len(report.records)
        report.ok = sum(r.status == STATUS_OK for r in report.records)
        report.fail = sum(r.status == STATUS_FAIL for r in report.records)
        report.unknown = sum(r.status == STATUS_UNKNOWN for r in report.records)
        return report

    async def run_forever(self) -> None:
        self.running = True
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            interval = int(settings.get("check_interval"))
            try:
                report = await self.run_cycle()
                self.last_report = report
                if report.records:
                    await self.on_cycle(report)
                log.info("check cycle", extra={
                    "duration": round(report.duration, 2), "checked": report.checked, "ok": report.ok,
                    "fail": report.fail, "unknown": report.unknown, "network_down": report.network_down,
                    "discarded": report.discarded, "skipped": report.skipped_reason,
                })
                if report.duration > interval:
                    log.warning("check cycle exceeded interval; next cycle will not overlap",
                                extra={"duration": round(report.duration, 1), "interval": interval})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("check cycle failed")
            # Next aligned slot after now: a slow cycle skips slots, never overlaps.
            now = loop.time()
            next_tick += interval
            if next_tick <= now:
                next_tick = now + interval - ((now - next_tick) % interval)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), max(0.0, next_tick - now))
                next_tick = loop.time()
            except asyncio.TimeoutError:
                pass
