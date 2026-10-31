"""Incident tracking: the N/M rule and flapping detection.

The same rule drives the stored ``Incident`` rows, the Telegram alerts and
the "offline" status on the public page:

* ``fail_threshold`` (N) consecutive failed checks open an incident.  Its
  start time is the first failure of the streak.
* ``recover_threshold`` (M) consecutive successful checks close it.  Its end
  time is the first success of the streak.
* ``unknown`` results (monitor network down, Xray unavailable) are ignored:
  they neither break nor extend a streak.

Flapping: more than ``flap_threshold`` transitions (down or up) within
``flap_window_minutes`` switches a server to "flapping" — one warning is
sent and individual down/up alerts are suppressed until the server has had
no transition for half the window; then a single "stabilised" message
reports its current state.  Incidents are still recorded while flapping.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

from app.db import db
from app.models import STATUS_FAIL, STATUS_OK, Check, Incident, Server
from app.services import events as ev
from app.services.checker import CheckRecord
from app.services.settings import settings
from app.timeutil import now_ts

log = logging.getLogger(__name__)


@dataclass
class TrackerState:
    consecutive_fail: int = 0
    consecutive_ok: int = 0
    fail_streak_start: int | None = None
    ok_streak_start: int | None = None
    in_incident: bool = False
    incident_started_at: int | None = None
    last_error: str | None = None


@dataclass
class Transition:
    kind: str  # "down" / "up"
    server_id: int
    at: int  # time of the check that triggered it
    started_at: int
    ended_at: int | None = None
    error_type: str | None = None

    @property
    def duration(self) -> int | None:
        return None if self.ended_at is None else max(0, self.ended_at - self.started_at)


class IncidentStateMachine:
    """Pure N/M logic (no I/O)."""

    def __init__(self, fail_threshold: int, recover_threshold: int) -> None:
        self.n = max(1, fail_threshold)
        self.m = max(1, recover_threshold)

    def feed(self, server_id: int, state: TrackerState, ts: int, status: str,
             error_type: str | None) -> Transition | None:
        if status == STATUS_FAIL:
            state.consecutive_ok, state.ok_streak_start = 0, None
            if state.consecutive_fail == 0:
                state.fail_streak_start = ts
            state.consecutive_fail += 1
            state.last_error = error_type
            if not state.in_incident and state.consecutive_fail >= self.n:
                state.in_incident = True
                state.incident_started_at = state.fail_streak_start or ts
                return Transition(ev.DOWN, server_id, ts, state.incident_started_at, error_type=error_type)
        elif status == STATUS_OK:
            state.consecutive_fail, state.fail_streak_start = 0, None
            if state.consecutive_ok == 0:
                state.ok_streak_start = ts
            state.consecutive_ok += 1
            if state.in_incident and state.consecutive_ok >= self.m:
                started = state.incident_started_at or ts
                ended = state.ok_streak_start or ts
                state.in_incident = False
                state.incident_started_at = None
                return Transition(ev.UP, server_id, ts, started, ended_at=ended, error_type=state.last_error)
        return None  # unknown: ignored


class FlapDetector:
    """Pure flapping detector over transition timestamps."""

    def __init__(self, window_seconds: int, threshold: int) -> None:
        self.window = window_seconds
        self.threshold = threshold
        self._transitions: dict[int, deque[int]] = {}
        self._flapping: set[int] = set()

    @property
    def calm_seconds(self) -> int:
        return self.window // 2

    def configure(self, window_seconds: int, threshold: int) -> None:
        self.window, self.threshold = window_seconds, threshold

    def seed(self, server_id: int, timestamps: list[int]) -> None:
        self._transitions[server_id] = deque(sorted(timestamps))

    def _prune(self, server_id: int, now: int) -> deque[int]:
        items = self._transitions.setdefault(server_id, deque())
        while items and items[0] < now - self.window:
            items.popleft()
        return items

    def record(self, server_id: int, ts: int) -> bool:
        """Register a transition.  Returns True when the server *starts* flapping."""
        items = self._prune(server_id, ts)
        items.append(ts)
        if server_id not in self._flapping and len(items) > self.threshold:
            self._flapping.add(server_id)
            return True
        return False

    def count(self, server_id: int, now: int) -> int:
        return len(self._prune(server_id, now))

    def is_flapping(self, server_id: int) -> bool:
        return server_id in self._flapping

    def check_calm(self, server_id: int, now: int) -> bool:
        """True (once) when a flapping server has been quiet long enough."""
        if server_id not in self._flapping:
            return False
        items = self._transitions.get(server_id) or deque()
        if not items or now - items[-1] >= self.calm_seconds:
            self._flapping.discard(server_id)
            return True
        return False

    def forget(self, server_id: int) -> None:
        self._transitions.pop(server_id, None)
        self._flapping.discard(server_id)


class IncidentService:
    """Applies the state machine to check results and persists incidents."""

    def __init__(self) -> None:
        self.states: dict[int, TrackerState] = {}
        self.flaps = FlapDetector(1800, 4)
        self._lock = threading.Lock()

    def _machine(self) -> IncidentStateMachine:
        return IncidentStateMachine(settings.get("fail_threshold"), settings.get("recover_threshold"))

    def _sync_flap_settings(self) -> None:
        self.flaps.configure(settings.get("flap_window_minutes") * 60, settings.get("flap_threshold"))

    def in_incident(self, server_id: int) -> bool:
        state = self.states.get(server_id)
        return bool(state and state.in_incident)

    def open_incident_start(self, server_id: int) -> int | None:
        state = self.states.get(server_id)
        return state.incident_started_at if state and state.in_incident else None

    # ------------------------------------------------------------ restore
    def restore(self, now: int | None = None) -> None:
        """Rebuild in-memory state from the DB after a restart (sync)."""
        now = now or now_ts()
        self._sync_flap_settings()
        machine = self._machine()
        lookback = max(machine.n, machine.m)
        with self._lock:
            self.states.clear()
            for incident in Incident.select().where(Incident.ended_at.is_null()):
                self.states[incident.server_id] = TrackerState(
                    in_incident=True, incident_started_at=incident.started_at, last_error=incident.error_type)
            server_ids = [s.id for s in Server.select(Server.id).where(Server.is_active == True)]  # noqa: E712
            for server_id in server_ids:
                state = self.states.setdefault(server_id, TrackerState())
                rows = list(
                    Check.select(Check.checked_at, Check.status, Check.error_type)
                    .where(Check.server == server_id, Check.status.in_([STATUS_OK, STATUS_FAIL]))
                    .order_by(Check.checked_at.desc()).limit(lookback).tuples()
                )
                # Count the trailing streak without emitting transitions.
                for ts, status, error in rows:
                    if status != rows[0][1]:
                        break
                    if status == STATUS_FAIL:
                        state.consecutive_fail += 1
                        state.fail_streak_start = ts
                        state.last_error = state.last_error or error
                    else:
                        state.consecutive_ok += 1
                        state.ok_streak_start = ts
            since = now - self.flaps.window
            stamps: dict[int, list[int]] = {}
            for incident in Incident.select().where((Incident.started_at >= since) | (Incident.ended_at >= since)):
                if incident.started_at >= since:
                    stamps.setdefault(incident.server_id, []).append(incident.started_at)
                if incident.ended_at and incident.ended_at >= since:
                    stamps.setdefault(incident.server_id, []).append(incident.ended_at)
            for server_id, values in stamps.items():
                self.flaps.seed(server_id, values)

    # ------------------------------------------------------------ process
    def process(self, records: list[CheckRecord], now: int | None = None) -> list[ev.Event]:
        """Feed results, persist incident changes, return notification events (sync)."""
        now = now or now_ts()
        self._sync_flap_settings()
        machine = self._machine()
        events: list[ev.Event] = []
        with self._lock, db.atomic():
            for record in sorted(records, key=lambda r: r.checked_at):
                state = self.states.setdefault(record.server_id, TrackerState())
                transition = machine.feed(record.server_id, state, record.checked_at, record.status,
                                          record.error_type)
                if transition is None:
                    continue
                self._persist(transition)
                started_flapping = self.flaps.record(record.server_id, transition.at)
                if started_flapping:
                    events.append(ev.Event(ev.FLAPPING, transition.at, record.server_id, {
                        "count": self.flaps.count(record.server_id, transition.at),
                        "window_minutes": self.flaps.window // 60,
                    }))
                elif not self.flaps.is_flapping(record.server_id):
                    events.append(self._transition_event(transition))
            for server_id in list(self.states):
                if self.flaps.check_calm(server_id, now):
                    events.append(ev.Event(ev.FLAP_END, now, server_id,
                                           {"in_incident": self.states[server_id].in_incident}))
        return events

    @staticmethod
    def _transition_event(t: Transition) -> ev.Event:
        data = {"started_at": t.started_at, "error_type": t.error_type}
        if t.kind == ev.UP:
            data.update(ended_at=t.ended_at, duration=t.duration)
        return ev.Event(t.kind, t.at, t.server_id, data)

    @staticmethod
    def _persist(t: Transition) -> None:
        if t.kind == ev.DOWN:
            Incident.create(server=t.server_id, started_at=t.started_at, error_type=t.error_type)
            return
        incident = (Incident.select().where(Incident.server == t.server_id, Incident.ended_at.is_null())
                    .order_by(Incident.started_at.desc()).first())
        if incident is None:
            log.warning("recovery without open incident", extra={"server_id": t.server_id})
            return
        incident.ended_at = t.ended_at
        incident.duration_seconds = t.duration
        incident.save()

    # ------------------------------------------------------------- admin
    def close_for_server(self, server_id: int, at: int | None = None) -> None:
        """Close an open incident silently (server deactivated / deleted)."""
        at = at or now_ts()
        with self._lock:
            for incident in Incident.select().where(Incident.server == server_id, Incident.ended_at.is_null()):
                incident.ended_at = at
                incident.duration_seconds = max(0, at - incident.started_at)
                incident.save()
            self.states.pop(server_id, None)
            self.flaps.forget(server_id)
