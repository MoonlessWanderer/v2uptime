import asyncio

import httpx
import pytest

from app.services import checker as checker_mod
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
