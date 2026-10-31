from app.services.proxy_pool import ProxyCandidate, select_candidates
from app.services.status import DEGRADED, OFFLINE, ONLINE, UNKNOWN


def c(sid, status=ONLINE, stability=90.0, latency=100.0):
    return ProxyCandidate(sid, f"s{sid}", 20000 + sid, status, stability, latency)


def ids(items):
    return [i.server_id for i in items]


def test_order_by_stability_then_latency():
    items = [c(1, stability=80), c(2, stability=95, latency=300), c(3, stability=95, latency=100)]
    assert ids(select_candidates(items)) == [3, 2, 1]


def test_preferred_first_only_if_online():
    items = [c(1, stability=99), c(2, stability=50), c(3, status=OFFLINE)]
    assert ids(select_candidates(items, preferred=2)) == [2, 1]
    assert ids(select_candidates(items, preferred=3)) == [1, 2]


def test_excludes_subject_server_and_unusable_states():
    items = [c(1), c(2), c(3, status=UNKNOWN), c(4, status=OFFLINE)]
    assert ids(select_candidates(items, exclude=[1])) == [2]


def test_degraded_used_as_last_resort():
    items = [c(1, status=DEGRADED, stability=99), c(2, stability=10)]
    assert ids(select_candidates(items)) == [2, 1]
    assert ids(select_candidates(items, include_degraded=False)) == [2]


def test_url_uses_socks5h():
    assert c(7).url == "socks5h://127.0.0.1:20007"
