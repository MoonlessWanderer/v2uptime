from app.models import Incident
from app.services import events as ev
from app.services.checker import CheckRecord
from app.services.incidents import FlapDetector, IncidentService, IncidentStateMachine, TrackerState

from tests.conftest import add_checks, make_server


def feed(machine, state, seq, start=1000, step=60):
    out = []
    for i, code in enumerate(seq):
        status = {"o": "ok", "f": "fail", "u": "unknown"}[code]
        t = machine.feed(1, state, start + i * step, status, None if status == "ok" else "timeout")
        if t:
            out.append(t)
    return out


def test_n_failures_open_incident_at_first_failure():
    m, s = IncidentStateMachine(3, 2), TrackerState()
    transitions = feed(m, s, "offf")
    assert [t.kind for t in transitions] == ["down"]
    assert transitions[0].started_at == 1060  # first failure of the streak
    assert s.in_incident


def test_fewer_than_n_failures_do_nothing():
    m, s = IncidentStateMachine(3, 2), TrackerState()
    assert feed(m, s, "ffoffo") == []
    assert not s.in_incident


def test_m_successes_close_incident_at_first_success():
    m, s = IncidentStateMachine(3, 2), TrackerState()
    transitions = feed(m, s, "fffofoo")
    assert [t.kind for t in transitions] == ["down", "up"]
    up = transitions[1]
    assert up.started_at == 1000 and up.ended_at == 1300 and up.duration == 300
    assert not s.in_incident


def test_unknown_results_are_ignored():
    m, s = IncidentStateMachine(3, 2), TrackerState()
    assert feed(m, s, "ffuuu") == []  # unknown does not count as a failure...
    assert [t.kind for t in feed(m, s, "f", start=5000)] == ["down"]  # ...nor reset the streak


def test_flap_detector():
    flaps = FlapDetector(window_seconds=1800, threshold=4)
    assert [flaps.record(1, t) for t in (0, 100, 200, 300)] == [False] * 4
    assert flaps.record(1, 400) is True  # 5th transition within 30 min
    assert flaps.record(1, 500) is False  # only reported once
    assert flaps.check_calm(1, 500 + 899) is False
    assert flaps.check_calm(1, 500 + 900) is True
    assert not flaps.is_flapping(1)


def test_old_transitions_leave_the_window():
    flaps = FlapDetector(window_seconds=1800, threshold=4)
    for t in (0, 100, 200, 300):
        flaps.record(1, t)
    assert flaps.record(1, 2500) is False


def records(server_id, seq, start=1000, step=60):
    return [CheckRecord(server_id, start + i * step, {"o": "ok", "f": "fail", "u": "unknown"}[c], None,
                        None if c == "o" else "timeout") for i, c in enumerate(seq)]


def test_service_persists_incidents_and_emits_events(database):
    server = make_server()
    service = IncidentService()
    events = service.process(records(server.id, "fffoo"), now=2000)
    assert [e.kind for e in events] == [ev.DOWN, ev.UP]
    incident = Incident.get()
    assert incident.started_at == 1000 and incident.ended_at == 1180 and incident.duration_seconds == 180


def test_service_suppresses_alerts_while_flapping(database):
    server = make_server()
    service = IncidentService()
    # each "fffoo" is one down + one up transition (2); thresholds: N=3, M=2, flap > 4
    events = service.process(records(server.id, "fffoo" * 3, step=30), now=1000 + 15 * 30)
    kinds = [e.kind for e in events]
    assert kinds == [ev.DOWN, ev.UP, ev.DOWN, ev.UP, ev.FLAPPING]
    assert Incident.select().count() == 3  # incidents still recorded
    # quiet for half the window -> one "stabilised" event
    later = service.process([], now=1000 + 15 * 30 + 900)
    assert [e.kind for e in later] == [ev.FLAP_END]


def test_restore_from_database(database):
    server = make_server()
    add_checks(server, 1000, "ooff")
    Incident.create(server=server.id, started_at=500)
    service = IncidentService()
    service.restore(now=2000)
    state = service.states[server.id]
    assert state.in_incident and state.consecutive_fail == 2 and state.consecutive_ok == 0
    events = service.process(records(server.id, "oo", start=3000), now=3100)
    assert [e.kind for e in events] == [ev.UP]


def test_close_for_server(database):
    server = make_server()
    Incident.create(server=server.id, started_at=500)
    service = IncidentService()
    service.restore(now=1000)
    service.close_for_server(server.id, at=900)
    assert Incident.get().ended_at == 900 and not service.in_incident(server.id)
