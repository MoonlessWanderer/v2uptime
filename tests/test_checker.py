import asyncio
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.services import checker as checker_mod
from app.services import net
from app.services.checker import (
    E_MONITOR_NET,
    E_RESET,
    E_TIMEOUT,
    E_TLS,
    E_XRAY,
    Checker,
    ProbeResult,
    apply_network_rule,
    classify_exception,
)

from tests.conftest import make_server

OK = ProbeResult(True, 120)
FAIL = ProbeResult(False, None, E_TIMEOUT, "timeout")
XRAY_DOWN = ProbeResult(False, None, E_XRAY, "refused")


def statuses(final):
    return {sid: status for sid, (status, _) in final.items()}


def test_all_fail_and_reference_fail_means_monitor_network_down():
    final, down = apply_network_rule({1: FAIL, 2: FAIL}, reference_ok=False)
    assert down is True
    assert statuses(final) == {1: "unknown", 2: "unknown"}
    assert all(r.error_type == E_MONITOR_NET for _, r in final.values())


def test_all_fail_but_reference_ok_are_real_failures():
    final, down = apply_network_rule({1: FAIL, 2: FAIL}, reference_ok=True)
    assert down is False and statuses(final) == {1: "fail", 2: "fail"}


def test_partial_failure_with_reference_down_is_not_network_outage():
    final, down = apply_network_rule({1: OK, 2: FAIL}, reference_ok=False)
    assert down is False and statuses(final) == {1: "ok", 2: "fail"}


def test_xray_side_errors_are_unknown_and_not_counted_for_outage():
    final, down = apply_network_rule({1: XRAY_DOWN, 2: XRAY_DOWN}, reference_ok=False)
    assert down is False and statuses(final) == {1: "unknown", 2: "unknown"}
    final, down = apply_network_rule({1: XRAY_DOWN, 2: FAIL}, reference_ok=False)
    assert down is True and statuses(final) == {1: "unknown", 2: "unknown"}


def test_classify_exception():
    assert classify_exception(httpx.ConnectTimeout("t"), True)[0] == E_TIMEOUT
    assert classify_exception(httpx.ConnectError("[Errno 111] Connection refused"), True)[0] == E_XRAY
    assert classify_exception(httpx.ConnectError("[Errno 111] Connection refused"), False)[0] == "connection_refused"
    assert classify_exception(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate"), True)[0] == E_TLS
    assert classify_exception(httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"), True)[0] == E_RESET
    assert classify_exception(httpx.ConnectError("[Errno -2] Name or service not known"), False)[0] == "dns"


class FakeXray:
    installed = True

    def __init__(self, ports):
        self.ports = ports
        self.epoch = 1

    async def wait_ready(self, timeout):
        return True

    def is_ready(self):
        return True

    def loaded_ports(self):
        return dict(self.ports)


def run_cycle_with(monkeypatch, database, xray, responses, reference_ok=True, bump_epoch=False):
    calls = {}
    monkeypatch.setattr(checker_mod.dnsdiag, "failure_kind", lambda address: None)

    async def fake_probe(url, proxy, timeout, accept_any_status=False):
        if proxy is None:
            return ProbeResult(reference_ok, 50 if reference_ok else None, None if reference_ok else E_TIMEOUT)
        port = int(proxy.rsplit(":", 1)[1])
        calls[port] = calls.get(port, 0) + 1
        if bump_epoch:
            xray.epoch += 1
        sequence = responses[port]
        return sequence[min(calls[port], len(sequence)) - 1]

    monkeypatch.setattr(checker_mod, "probe", fake_probe)
    checker = Checker(xray, on_cycle=None)
    return asyncio.run(checker.run_cycle()), calls


def test_cycle_retries_once_and_records_final(monkeypatch, database):
    xray = FakeXray({1: 20001, 2: 20002})
    report, calls = run_cycle_with(monkeypatch, database, xray, {20001: [FAIL, OK], 20002: [OK]})
    assert calls == {20001: 2, 20002: 1}
    by_id = {r.server_id: r for r in report.records}
    assert by_id[1].status == "ok" and by_id[2].status == "ok"
    assert report.checked == 2 and report.fail == 0


def test_cycle_discarded_when_xray_restarts(monkeypatch, database):
    xray = FakeXray({1: 20001})
    report, _ = run_cycle_with(monkeypatch, database, xray, {20001: [OK]}, bump_epoch=True)
    assert report.discarded and report.records == []


def test_cycle_network_down(monkeypatch, database):
    xray = FakeXray({1: 20001, 2: 20002})
    report, _ = run_cycle_with(monkeypatch, database, xray, {20001: [FAIL], 20002: [FAIL]}, reference_ok=False)
    assert report.network_down and report.unknown == 2


def test_ssl_context_built_once(monkeypatch):
    calls = []
    original = net.ssl.create_default_context

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(net.ssl, "create_default_context", counting)
    net.ssl_context.cache_clear()
    try:
        clients = [net.async_client() for _ in range(5)]
        assert len(calls) == 1
        assert all(c._transport._pool._ssl_context is net.ssl_context() for c in clients)
    finally:
        net.ssl_context.cache_clear()


def test_client_setup_is_not_counted_as_latency(monkeypatch):
    original = httpx.AsyncClient

    class SlowSetupClient(original):
        def __init__(self, *args, **kwargs):
            time.sleep(0.3)  # e.g. loading a CA bundle
            kwargs.pop("proxy", None)
            super().__init__(*args, transport=httpx.MockTransport(lambda request: httpx.Response(204)), **kwargs)

    monkeypatch.setattr(checker_mod.httpx, "AsyncClient", SlowSetupClient)
    result = asyncio.run(checker_mod.probe("https://x.test/", None, 5))
    assert result.ok and result.latency_ms < 150


@pytest.mark.parametrize("status_code, accept_any, ok", [(204, False, True), (200, False, True),
                                                         (302, False, False), (404, True, True)])
def test_probe_status_handling(monkeypatch, status_code, accept_any, ok):
    def handler(request):
        return httpx.Response(status_code)

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs.pop("proxy", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(checker_mod.httpx, "AsyncClient", MockClient)
    result = asyncio.run(checker_mod.probe("https://x.test/", None, 5, accept_any_status=accept_any))
    assert result.ok is ok
    if not ok:
        assert result.error_type == "http_status"


# ------------------------------------------------- cold + warm requests

class _ConnectCostHandler(BaseHTTPRequestHandler):
    """Delays only the first request of each connection (the cost of opening it)."""

    protocol_version = "HTTP/1.1"  # keep-alive

    def setup(self):
        super().setup()
        self.served = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.served += 1
        self.server.counts[self.client_address] = self.served
        if self.served == 1:
            time.sleep(0.3)
        elif self.server.mode == "drop":  # second request: close without answering
            self.close_connection = True
            return
        elif self.server.mode == "hang":
            time.sleep(3)
        if self.server.mode == "slow-body":  # headers at once, body 3 seconds later
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.flush()
            time.sleep(3)
            self.wfile.write(b"ok")
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("closed")
        data += chunk
    return data


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _socks5_handle(client):
    """No-auth SOCKS5 CONNECT, standing in for an Xray SOCKS inbound."""
    try:
        greeting = _recv_exact(client, 2)
        _recv_exact(client, greeting[1])
        client.sendall(b"\x05\x00")
        _ver, _cmd, _rsv, atyp = _recv_exact(client, 4)
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(client, 4))
        else:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode()
        port = int.from_bytes(_recv_exact(client, 2), "big")
        upstream = socket.create_connection((host, port))
        client.sendall(b"\x05\x00\x00\x01" + bytes(6))
    except OSError:
        client.close()
        return
    threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
    _pipe(upstream, client)


@pytest.fixture
def connect_cost_server():
    servers = []

    def start(mode="ok"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ConnectCostHandler)
        server.daemon_threads, server.block_on_close = True, False
        server.mode, server.counts = mode, {}
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server, f"http://127.0.0.1:{server.server_address[1]}/generate_204"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def socks_port():
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=_socks5_handle, args=(client,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1]
    listener.close()


def test_ping_is_the_warm_request_and_connect_ms_the_cold_one(connect_cost_server, socks_port):
    server, url = connect_cost_server()
    checker = Checker(FakeXray({1: socks_port}), on_cycle=None)
    result = asyncio.run(checker.check_server(socks_port, 5, url))  # through SOCKS, like production
    assert result.ok and result.warm and result.status_code == 204
    assert result.connect_ms >= 300
    assert result.latency_ms < 100


def test_both_requests_share_one_connection(connect_cost_server, socks_port):
    server, url = connect_cost_server()
    for _ in range(3):
        assert asyncio.run(checker_mod.probe(url, checker_mod.socks_url(socks_port), 5)).ok
    assert list(server.counts.values()) == [2, 2, 2]  # one connection per check, two requests on each


def test_reference_check_stays_one_cold_request(connect_cost_server):
    server, url = connect_cost_server()
    result = asyncio.run(checker_mod.probe(url, None, 5, accept_any_status=True))
    assert result.ok and not result.warm
    assert result.latency_ms == result.connect_ms >= 300
    assert list(server.counts.values()) == [1]


def test_reference_check_does_not_wait_for_the_body(connect_cost_server):
    _server, url = connect_cost_server("slow-body")
    started = time.perf_counter()
    result = asyncio.run(checker_mod.probe(url, None, 5, accept_any_status=True))
    assert result.ok and result.status_code == 200
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize("mode", ["drop", "hang"])
def test_failed_warm_request_falls_back_to_cold_time(connect_cost_server, mode, caplog):
    server, url = connect_cost_server(mode)
    timeout = 0.8
    started = time.perf_counter()
    with caplog.at_level(logging.DEBUG, logger="app.services.checker"):
        result = asyncio.run(checker_mod.probe(url, None, timeout, warm=True))
    elapsed = time.perf_counter() - started
    assert result.ok and not result.warm
    assert result.latency_ms == result.connect_ms >= 300
    assert elapsed < 2 * timeout
    assert any("warm request" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


def test_cycle_records_connect_ms(monkeypatch, database):
    from app.models import Check
    from app.services.monitor import save_checks

    xray = FakeXray({1: 20001, 2: 20002})
    make_server("A", port=20001), make_server("B", port=20002)
    report, _ = run_cycle_with(monkeypatch, database, xray, {
        20001: [ProbeResult(True, 140, status_code=204, connect_ms=520, warm=True)],
        20002: [FAIL, FAIL]})
    by_id = {r.server_id: r for r in report.records}
    assert (by_id[1].latency_ms, by_id[1].connect_ms) == (140, 520)
    assert (by_id[2].latency_ms, by_id[2].connect_ms) == (None, None)
    save_checks(report.records)
    assert {c.server_id: c.connect_ms for c in Check.select()} == {1: 520, 2: None}


def test_failed_domain_check_is_labeled_from_dns(monkeypatch, database):
    server = make_server("Dom", address="cdn.example.com")
    xray = FakeXray({server.id: 20001})

    async def fake_probe(url, proxy, timeout, accept_any_status=False):
        if proxy is None:
            return ProbeResult(True, 20)
        return FAIL

    monkeypatch.setattr(checker_mod, "probe", fake_probe)
    monkeypatch.setattr(checker_mod.dnsdiag, "failure_kind",
                        lambda address: "dns_blocked" if address == "cdn.example.com" else None)
    report = asyncio.run(Checker(xray, on_cycle=None).run_cycle())
    assert report.records[0].error_type == "dns_blocked"
    assert report.records[0].status == "fail"


def test_failed_ip_check_keeps_its_error(monkeypatch, database):
    server = make_server("IP", address="1.2.3.4")
    xray = FakeXray({server.id: 20001})
    called = []

    async def fake_probe(url, proxy, timeout, accept_any_status=False):
        if proxy is None:
            return ProbeResult(True, 20)
        return FAIL

    monkeypatch.setattr(checker_mod, "probe", fake_probe)
    monkeypatch.setattr(checker_mod.dnsdiag, "failure_kind", lambda address: called.append(address) or "dns")
    report = asyncio.run(Checker(xray, on_cycle=None).run_cycle())
    assert called == []
    assert report.records[0].error_type == E_TIMEOUT


def test_monitor_outage_overrides_a_dns_label(monkeypatch, database):
    server = make_server("Dom", address="cdn.example.com")
    xray = FakeXray({server.id: 20001})

    async def fake_probe(url, proxy, timeout, accept_any_status=False):
        return FAIL

    monkeypatch.setattr(checker_mod, "probe", fake_probe)
    monkeypatch.setattr(checker_mod.dnsdiag, "failure_kind", lambda address: "dns")
    report = asyncio.run(Checker(xray, on_cycle=None).run_cycle())
    assert report.network_down
    assert report.records[0].error_type == E_MONITOR_NET


def test_probe_command_prints_cold_warm_and_phases(connect_cost_server, socks_port, database, monkeypatch, capsys):
    from app import cli
    from app.services.settings import settings

    _server, url = connect_cost_server()
    server = make_server("🇩🇪 Germany", port=socks_port)
    settings.set("test_url", url)
    monkeypatch.setattr(cli, "_open_db", lambda: None)  # the test database is already open
    from app.services.dns import Resolution
    monkeypatch.setattr("app.services.dns.resolve",
                        lambda name: Resolution(["203.0.113.5"], "system", 1_700_000_000))
    monkeypatch.setattr("app.services.dns.failure_kind", lambda address: None)
    cli.main(["probe", str(server.id), "--count", "2"])
    out = capsys.readouterr().out
    assert "Server 1: 🇩🇪 Germany (vless/tcp/none)" in out
    assert "DNS: 203.0.113.5 via system" in out
    lines = [line for line in out.splitlines() if line.startswith("#")]
    assert len(lines) == 2
    for line in lines:
        assert "cold " in line and "warm " in line and "HTTP 204" in line
        assert "tcp " in line and "socks " in line and "ttfb " in line
    assert out.splitlines()[-2].startswith("Median: cold ")
