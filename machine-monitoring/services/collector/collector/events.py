"""Turn evaluations into higher-level events.

This is the v0.1 home of the event-engine logic (RUN_START, RUN_STOP,
FAULT_START, FAULT_CLEAR, counter changes, downtime periods). It runs
in-process in the collector so the first end-to-end path has no extra
moving parts; it is written as a pure function of evaluations so it can
move into services/event-engine (consuming MQTT) without changes.

Downtime/fault operations are *idempotent ensure/close* operations so a
collector restart in the middle of a fault does not split or duplicate
the period in the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import GOOD, UNCERTAIN, UNKNOWN, Evaluation

DOWNTIME_STATES = {"OFF", "READY", "IDLE", "STOPPED", "FAULT", "E_STOP"}
COUNTERS = ("counter.total", "counter.good", "counter.reject")


@dataclass
class StateChange:
    time: float
    previous: str | None
    new: str
    reason: str
    quality: str
    snapshot: dict[str, Any]


@dataclass
class MachineEvent:
    time: float
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CounterSample:
    time: float
    counter: str
    value: int
    delta: int | None


@dataclass
class DowntimeEnsure:
    time: float
    state: str
    reason: str


@dataclass
class DowntimeClose:
    time: float


@dataclass
class FaultEnsure:
    time: float
    code: int | None


@dataclass
class FaultClose:
    time: float


Op = StateChange | MachineEvent | CounterSample | DowntimeEnsure | DowntimeClose | FaultEnsure | FaultClose


def _known(ev: Evaluation, name: str) -> Any:
    nv = ev.normalized.get(name)
    if nv is None or nv.quality not in (GOOD, UNCERTAIN):
        return None
    return nv.value


class EventTracker:
    def __init__(self, last_counters: dict[str, int] | None = None) -> None:
        self.state: str | None = None
        self.fault: bool | None = None
        self.fault_code: int | None = None
        self.estop: bool | None = None
        self.counters: dict[str, int] = dict(last_counters or {})

    def process(self, ev: Evaluation, now: float) -> list[Op]:
        ops: list[Op] = []
        prev, new = self.state, ev.state

        if new != prev:
            ops.append(StateChange(now, prev, new, ev.reason if prev is not None else f"collector start: {ev.reason}",
                                   ev.state_quality, ev.state_inputs))
            if new == UNKNOWN and prev is not None:
                ops.append(MachineEvent(now, "MONITORING_LOST", {"reason": ev.reason, "previous": prev}))
            if prev == UNKNOWN and new != UNKNOWN:
                ops.append(MachineEvent(now, "MONITORING_RESTORED", {"state": new}))
            if new == "RUNNING":
                ops.append(MachineEvent(now, "RUN_START", {"previous": prev}))
            if prev == "RUNNING" and new != UNKNOWN:
                ops.append(MachineEvent(now, "RUN_STOP", {"new": new, "reason": ev.reason}))
            # Downtime only changes on *known* states; UNKNOWN is a monitoring gap.
            if new == "RUNNING":
                ops.append(DowntimeClose(now))
            elif new in DOWNTIME_STATES:
                ops.append(DowntimeEnsure(now, new, ev.reason))
            self.state = new

        fault = _known(ev, "machine.fault")
        code = _known(ev, "machine.fault_code")
        code = int(code) if code else None  # 0 = "no code (yet)": fault bit and code may arrive in different polls
        if fault is not None:
            fault = bool(fault)
            if fault and self.fault is not True:
                ops.append(MachineEvent(now, "FAULT_START", {"code": code}))
                ops.append(FaultEnsure(now, code))
            elif fault and code is not None and code != self.fault_code:
                # Code arrived late (fills the open fault) or changed (new fault period).
                ops.append(FaultEnsure(now, code))
                if self.fault_code is not None:
                    ops.append(MachineEvent(now, "FAULT_CODE_CHANGE", {"from": self.fault_code, "to": code}))
            elif not fault and self.fault is True:
                ops.append(MachineEvent(now, "FAULT_CLEAR", {"code": self.fault_code}))
                ops.append(FaultClose(now))
            elif not fault and self.fault is None:
                ops.append(FaultClose(now))  # close anything left open by a previous run
            if fault:
                self.fault_code = code if code is not None else self.fault_code
            else:
                self.fault_code = None
            self.fault = fault

        estop = _known(ev, "machine.estop")
        if estop is not None:
            estop = bool(estop)
            if self.estop is not None and estop != self.estop:
                ops.append(MachineEvent(now, "ESTOP_START" if estop else "ESTOP_CLEAR"))
            self.estop = estop

        for name in COUNTERS:
            value = _known(ev, name)
            if value is None:
                continue
            value = int(value)
            last = self.counters.get(name)
            if last == value:
                continue
            delta: int | None
            if last is None:
                delta = None
            elif value < last:
                delta = value
                ops.append(MachineEvent(now, "COUNTER_RESET", {"counter": name, "from": last, "to": value}))
            else:
                delta = value - last
            ops.append(CounterSample(now, name, value, delta))
            self.counters[name] = value

        return ops
